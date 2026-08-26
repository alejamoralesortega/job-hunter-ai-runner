"""Puntúa el match entre una oferta de empleo y el CV base usando Gemini (free tier)."""

import json
import os
import re
import time

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


def score_job(job, api_key, cv_text=None):
    """Devuelve dict {"score": int 0-100, "justificacion": str} para una oferta. Si no se pasa
    `cv_text` (modo multi-usuario, el CV de cada perfil), usa el CV fijo de data/base_cv.md
    (modo single-user de siempre)."""
    cv = cv_text or load_base_cv()
    prompt = f"""Eres un reclutador técnico experto. Compara el siguiente CV con la oferta de
empleo y responde SOLO con un JSON válido (sin texto adicional, sin markdown) con este formato:
{{"score": <entero 0-100>, "justificacion": "<máximo 2 frases explicando el match>"}}

El score debe reflejar qué tan buen candidato es esta persona para este puesto específico,
considerando stack técnico, seniority y responsabilidades.

Además del match técnico, aplica esta prioridad geográfica/idioma como un factor que sube o baja
el score (el candidato vive en Bogotá, Colombia):
- PRIORIDAD ALTA: el puesto es remoto para candidatos en Colombia, remoto para Latinoamérica
  (explícitamente incluye Colombia o LatAm), o es híbrido/presencial en Bogotá. Estos deben recibir
  el score más alto que el match técnico permita.
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
