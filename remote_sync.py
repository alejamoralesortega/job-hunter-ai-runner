"""Cliente HTTP hacia la API del dashboard de Job Hunter AI -- reemplaza a supabase_sync.py para
el runner que corre en el propio GitHub del usuario. Este runner nunca ve la llave maestra de
Supabase ni el token del bot de Telegram: todo pasa por estos dos endpoints, autenticados con un
token propio (DASHBOARD_API_TOKEN) que solo puede leer/escribir los datos de este usuario."""

import base64
import os

import requests


def _headers(api_token):
    return {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}


def get_context(api_base, api_token):
    """Trae todo lo necesario para un ciclo en un solo GET: credenciales ya descifradas, cargos,
    texto del CV ya extraído, config de auto-apply, ids ya procesados y el flag de pausa."""
    resp = requests.get(f"{api_base}/api/cron/context", headers=_headers(api_token), timeout=30)
    resp.raise_for_status()
    return resp.json()


def report_job(api_base, api_token, job, score_result, estado, cv_pdf_path=None, apply_reason=None):
    """Reporta el resultado de una oferta procesada. Si `cv_pdf_path` existe, se manda en base64
    dentro del mismo POST -- el dashboard lo sube al bucket cv-adaptados y, si `estado` es
    "Aplicado", dispara el aviso de Telegram con el bot central (este runner nunca lo tiene)."""
    cv_pdf_base64 = None
    if cv_pdf_path and os.path.exists(cv_pdf_path):
        with open(cv_pdf_path, "rb") as f:
            cv_pdf_base64 = base64.b64encode(f.read()).decode("ascii")

    body = {
        "job": {
            "titulo": job["titulo"],
            "empresa": job["empresa"],
            "url": job.get("url"),
            "id_externo": job["id_externo"],
            "fuente": job["fuente"],
            "fecha": job.get("fecha"),
        },
        "scoreResult": score_result,
        "estado": estado,
        "cvPdfBase64": cv_pdf_base64,
        "applyReason": apply_reason,
    }
    resp = requests.post(f"{api_base}/api/cron/report", headers=_headers(api_token), json=body, timeout=60)
    resp.raise_for_status()
    return resp.json()
