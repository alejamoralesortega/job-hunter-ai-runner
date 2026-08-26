"""Genera un CV adaptado (Markdown -> HTML -> PDF) para una oferta puntual, usando Gemini."""

import os
import re

import markdown as md
from jinja2 import Environment, FileSystemLoader
from xhtml2pdf import pisa

from score_match import _call_gemini, load_base_cv

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "generated_cvs")


def _strip_code_fence(text):
    text = text.strip()
    text = re.sub(r"^```(markdown|md)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    return text.strip()


def tailor_cv_markdown(job, api_key, cv_text=None):
    cv = cv_text or load_base_cv()
    prompt = f"""Eres un experto en redacción de CVs técnicos. Tienes el CV base de un candidato
y una oferta de empleo puntual. Debes adaptar el CV para esa oferta:

- Reescribe el "Perfil profesional" (2-4 líneas) para resaltar lo más relevante a esta oferta.
- Reordena la lista de habilidades técnicas poniendo primero las más relevantes al puesto.
- NO inventes experiencia, habilidades, empresas ni logros que no estén en el CV base.
- Mantén el resto del contenido (experiencia, proyectos, educación) fiel a los hechos del CV base,
  puedes reescribir bullets levemente para enfatizar lo relevante pero sin inventar datos.
- Responde ÚNICAMENTE con el CV completo en formato Markdown, con la misma estructura de
  encabezados (#, ##) que el CV base. No agregues explicaciones ni code fences.

--- CV BASE ---
{cv}

--- OFERTA ---
Título: {job['titulo']}
Empresa: {job['empresa']}
Ubicación: {job.get('ubicacion', '')}
Descripción: {job['descripcion']}
"""
    text = _call_gemini(prompt, api_key)
    return _strip_code_fence(text)


def markdown_to_pdf(markdown_text, output_path):
    html_body = md.markdown(markdown_text, extensions=["tables", "sane_lists"])
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))
    template = env.get_template("cv_template.html")
    full_html = template.render(content=html_body)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "wb") as f:
        result = pisa.CreatePDF(full_html, dest=f)
    if result.err:
        raise RuntimeError(f"Error generando PDF: {output_path}")
    return output_path


def generate_tailored_cv(job, api_key, cv_text=None):
    """Genera el CV adaptado para `job` y devuelve la ruta del PDF generado."""
    markdown_text = tailor_cv_markdown(job, api_key, cv_text)
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", job["id_externo"])
    output_path = os.path.join(OUTPUT_DIR, f"{safe_id}.pdf")
    return markdown_to_pdf(markdown_text, output_path)


if __name__ == "__main__":
    demo_job = {
        "id_externo": "demo-1",
        "titulo": "Backend Python Developer",
        "empresa": "Acme Corp",
        "ubicacion": "Remote",
        "descripcion": "Buscamos backend developer con experiencia en Python, AWS Lambda y DynamoDB.",
    }
    key = os.environ["GEMINI_API_KEY"]
    path = generate_tailored_cv(demo_job, key)
    print(f"CV generado en: {path}")
