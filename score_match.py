"""Puntúa el match entre una oferta de empleo y el CV base usando Gemini (free tier)."""

import json
import os
import re
import time
import unicodedata

import requests

BASE_CV_PATH = os.path.join(os.path.dirname(__file__), "data", "base_cv.md")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)

_base_cv_cache = None


def load_base_cv():
    global _base_cv_cache
    if _base_cv_cache is None:
        with open(BASE_CV_PATH, "r", encoding="utf-8") as f:
            _base_cv_cache = f.read()
    return _base_cv_cache


_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _call_gemini(prompt, api_key, max_retries=3, backoff_seconds=20):
    """Llama a Gemini. Si el free tier responde 429 (rate limit), un 5xx transitorio (503
    "Service Unavailable" es común y pasó en producción -- Gemini con carga alta, nada que ver
    con la oferta) o hay un timeout/error de red, espera con backoff y reintenta en vez de darle
    un score 0 injusto a una oferta que ni siquiera se llegó a evaluar."""
    delay = backoff_seconds
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                GEMINI_URL,
                params={"key": api_key},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=60,
            )
        except requests.RequestException as e:
            if attempt < max_retries:
                print(f"[gemini] error de red ({e}), reintentando en {delay}s (intento {attempt + 1}/{max_retries})...")
                time.sleep(delay)
                delay *= 2
                continue
            raise
        if resp.status_code in _RETRYABLE_STATUS_CODES and attempt < max_retries:
            print(f"[gemini] {resp.status_code}, reintentando en {delay}s (intento {attempt + 1}/{max_retries})...")
            time.sleep(delay)
            delay *= 2
            continue
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]


def _extract_json(text):
    """Gemini a veces envuelve el JSON en ```json ... ```; lo limpiamos."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No se encontró JSON en la respuesta de Gemini: {text[:200]}")
    return json.loads(match.group(0))


_MODALIDADES_VALIDAS = {"Remoto", "Híbrido", "Presencial"}


def _modalidad_prompt(modalidades):
    """Sección extra del prompt para respetar la modalidad de trabajo que el candidato eligió en
    Ajustes. Si eligió las 3 (o no configuró nada), no cambia nada del comportamiento de siempre
    -- solo cuando excluyó alguna modalidad vale la pena penalizar por eso."""
    if not modalidades:
        return ""
    elegidas = sorted({m for m in modalidades if m in _MODALIDADES_VALIDAS})
    if not elegidas or len(elegidas) == len(_MODALIDADES_VALIDAS):
        return ""
    return f"""
El candidato SOLO quiere considerar ofertas con esta modalidad de trabajo: {", ".join(elegidas)}.
- Si la oferta es claramente de una modalidad que el candidato NO eligió (ej. es presencial y el
  candidato no marcó "Presencial"), el score debe bajar considerablemente (máximo ~20 puntos),
  aunque el match técnico sea excelente.
- Si la modalidad de la oferta coincide con alguna de las elegidas, no la penalices por esto.
- Si la descripción no aclara la modalidad, no penalices por este punto -- dale el beneficio de
  la duda.
"""


# Capitales de departamento de Colombia -- mismo listado que CIUDADES_COLOMBIA en
# dashboard/src/lib/types.ts (el desplegable de Ajustes), duplicado acá porque un lado es
# TypeScript y el otro Python -- no hay forma de compartir el array literal entre los dos.
CIUDADES_COLOMBIA = [
    "Bogotá", "Medellín", "Cali", "Barranquilla", "Cartagena", "Cúcuta", "Bucaramanga",
    "Ibagué", "Pereira", "Santa Marta", "Villavicencio", "Manizales", "Neiva", "Pasto",
    "Armenia", "Valledupar", "Montería", "Sincelejo", "Popayán", "Tunja", "Florencia",
    "Riohacha", "Yopal", "Quibdó", "Mocoa", "San Andrés", "Leticia", "Arauca", "Inírida",
    "Mitú", "Puerto Carreño",
]

_REMOTO_KEYWORDS = [
    "remoto", "remote", "home office", "trabajo desde casa", "100% virtual", "teletrabajo",
]


def _normalizar(text):
    text = (text or "").lower()
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def es_ubicacion_compatible(job, ciudad):
    """Filtra ANTES de gastar una llamada a Gemini: si la oferta menciona una ciudad de Colombia
    DISTINTA a la del candidato, y no hay ninguna señal de que sea remota (ni en la ubicación, ni
    en el título, ni en la descripción), se descarta sin evaluar -- confirmado con un caso real
    en producción: una oferta presencial en Cali para un candidato en Bogotá, sin ninguna palabra
    "remoto" en ningún campo.

    Sin `ciudad` configurada, no filtra nada (comportamiento de siempre, todo pasa a Gemini). Si
    la oferta no menciona NINGUNA ciudad conocida, tampoco filtra -- mejor dejar que Gemini decida
    con el resto del contexto (como hace hoy) que descartar a ciegas por texto ambiguo.
    """
    if not ciudad:
        return True

    texto = _normalizar(f"{job.get('ubicacion', '')} {job.get('titulo', '')} {job.get('descripcion', '')}")

    if any(_normalizar(kw) in texto for kw in _REMOTO_KEYWORDS):
        return True

    ciudad_norm = _normalizar(ciudad)
    if ciudad_norm in texto:
        return True

    otras_ciudades = [c for c in CIUDADES_COLOMBIA if _normalizar(c) != ciudad_norm]
    menciona_otra_ciudad = any(_normalizar(c) in texto for c in otras_ciudades)
    return not menciona_otra_ciudad


def score_job(job, api_key, cv_text=None, modalidades=None, ciudad=None):
    """Devuelve dict {"score": int 0-100, "justificacion": str} para una oferta. Si no se pasa
    `cv_text` (modo multi-usuario, el CV de cada perfil), usa el CV fijo de data/base_cv.md
    (modo single-user de siempre). `modalidades` es la lista de modalidades de trabajo que el
    candidato eligió en Ajustes (Remoto/Híbrido/Presencial) -- None o las 3 juntas equivale a
    "sin preferencia", el comportamiento de siempre. `ciudad` es la ciudad configurada en
    Ajustes -- si no se pasa, se asume Bogotá (el valor que estuvo hardcodeado para todos los
    usuarios hasta que se agregó el campo)."""
    cv = cv_text or load_base_cv()
    ciudad_candidato = ciudad or "Bogotá"
    prompt = f"""Eres un reclutador técnico experto. Compara el siguiente CV con la oferta de
empleo y responde SOLO con un JSON válido (sin texto adicional, sin markdown) con este formato:
{{"score": <entero 0-100>, "justificacion": "<máximo 2 frases explicando el match>"}}

El score debe reflejar qué tan buen candidato es esta persona para este puesto específico,
considerando stack técnico, seniority y responsabilidades.

Además del match técnico, aplica esta prioridad geográfica/idioma como un factor que sube o baja
el score (el candidato vive en {ciudad_candidato}, Colombia):
- PRIORIDAD ALTA: el puesto es remoto para candidatos en Colombia, remoto para Latinoamérica
  (explícitamente incluye Colombia o LatAm), o es híbrido/presencial en {ciudad_candidato}. Estos
  deben recibir el score más alto que el match técnico permita.
- PRIORIDAD MEDIA: la oferta no aclara restricción geográfica (remoto "worldwide"/global sin
  exclusiones), o el equipo de trabajo es en español.
- PRIORIDAD BAJA: el puesto es remoto solo para EE.UU., Canadá, Europa u otra región específica
  que NO incluye Colombia/LatAm, exige zona horaria incompatible (ej. solo horario EST/PST/CET), o
  requiere autorización de trabajo en un país donde el candidato no la tiene. NO descartes estas
  ofertas ni les des 0 automáticamente, pero su score debe quedar notablemente más bajo que uno
  de prioridad alta con el mismo nivel de match técnico — resta aproximadamente 20-30 puntos frente
  a lo que el match técnico solo ameritaría, salvo que el match sea excepcional (senior exacto,
  stack idéntico).
Si la descripción no menciona nada sobre ubicación/restricciones de contratación, asume
PRIORIDAD MEDIA.
{_modalidad_prompt(modalidades)}
--- CV ---
{cv}

--- OFERTA ---
Título: {job['titulo']}
Empresa: {job['empresa']}
Ubicación: {job.get('ubicacion', '')}
Descripción: {job['descripcion']}
"""
    try:
        text = _call_gemini(prompt, api_key)
        result = _extract_json(text)
        score = int(result.get("score", 0))
        justificacion = str(result.get("justificacion", "")).strip()
        return {"score": max(0, min(100, score)), "justificacion": justificacion}
    except Exception as e:
        print(f"[score_match] error puntuando '{job['titulo']}': {e}")
        return {"score": 0, "justificacion": "Error al evaluar con IA."}


if __name__ == "__main__":
    demo_job = {
        "titulo": "Backend Python Developer",
        "empresa": "Acme Corp",
        "ubicacion": "Remote",
        "descripcion": "Buscamos backend developer con experiencia en Python, AWS Lambda y DynamoDB.",
    }
    key = os.environ["GEMINI_API_KEY"]
    print(score_job(demo_job, key))
