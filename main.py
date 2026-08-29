"""Runner de Job Hunter AI para UN usuario, corriendo en su propia cuenta de GitHub (Actions
gratis en repos públicos, 45 min completos cada 4h, sin competir por tiempo con nadie más).

A diferencia del cron central (repo privado), este runner nunca recibe la llave maestra de
Supabase ni el token del bot de Telegram -- todo pasa por remote_sync.py, autenticado con
DASHBOARD_API_TOKEN (secreto propio de este repo, scoped solo a los datos de este usuario en el
dashboard). Trae su propia GEMINI_API_KEY (gratis, Google AI Studio) para no depender del cupo
de nadie más.
"""

import os
import sys
import time
import unicodedata

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

from auto_apply import (
    apply_to_computrabajo,
    apply_to_elempleo,
    apply_to_manpowergroup,
    apply_to_trabajoscom,
)
from fetch_jobs import fetch_jobs_for_platforms
from generate_cv import generate_tailored_cv
from remote_sync import get_context, report_job
from score_match import es_ubicacion_compatible, score_job

load_dotenv()

APPLY_FUNCTIONS = {
    "ElEmpleo": apply_to_elempleo,
    "Computrabajo": apply_to_computrabajo,
    "Trabajos.com": apply_to_trabajoscom,
    "ManpowerGroup": apply_to_manpowergroup,
}

GEMINI_SLEEP_SECONDS = float(os.environ.get("GEMINI_SLEEP_SECONDS", "15"))
# Un solo usuario por repo -- no hay que repartir con nadie, se usa el ciclo completo (con margen
# frente al timeout-minutes del workflow para dejar tiempo de fetch/setup/summary).
TIME_BUDGET_SECONDS = float(os.environ.get("TIME_BUDGET_SECONDS", str(40 * 60)))


def _normalize(text):
    text = (text or "").lower()
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def _es_elegible(titulo, excluir_keywords):
    """Filtra por título ANTES de gastar una llamada a Gemini (ej. ofertas de practicante/
    aprendiz para las que el candidato ya no es elegible)."""
    titulo_norm = _normalize(titulo)
    return not any(_normalize(kw) in titulo_norm for kw in excluir_keywords)


def run():
    api_base = os.environ["DASHBOARD_API_BASE"].rstrip("/")
    api_token = os.environ["DASHBOARD_API_TOKEN"]
    gemini_key = os.environ["GEMINI_API_KEY"]

    context = get_context(api_base, api_token)

    if context.get("pausado"):
        print("[main] Automatización pausada desde el dashboard -- se omite esta corrida.")
        return

    cargos = context.get("cargos") or []
    credenciales = context.get("credenciales") or {}
    cv_text = context.get("cvText")
    if not cargos or not credenciales or not cv_text:
        print("[main] Perfil incompleto (cargos, credenciales de plataforma o CV legible) -- se omite esta corrida.")
        return

    existing_ids = set(context.get("existingIds") or [])
    excluir_keywords = context.get("excluirTituloKeywords") or []
    score_threshold = context.get("scoreThreshold", 70)
    modalidades = context.get("modalidades") or ["Remoto", "Híbrido", "Presencial"]
    ciudad = context.get("ciudad")
    auto_apply_config = {"auto_apply_answers": context.get("autoApplyAnswers") or {}}

    jobs, fuentes_caidas = fetch_jobs_for_platforms(cargos, list(credenciales.keys()))
    if fuentes_caidas:
        print(f"[main] fuentes caídas/rotas este ciclo: {', '.join(fuentes_caidas)}")

    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    stats = {
        "auto_aplicadas": 0,
        "no_auto_aplicables": 0,
        "descartadas": 0,
        "no_elegibles": 0,
        "fuera_de_ciudad": 0,
        "ya_procesadas": 0,
        "errores": 0,
        "sin_tiempo": 0,
    }

    for job in jobs:
        if job["id_externo"] in existing_ids:
            stats["ya_procesadas"] += 1
            continue

        if time.monotonic() >= deadline:
            stats["sin_tiempo"] += 1
            continue

        if job["fuente"] not in credenciales:
            continue

        if not _es_elegible(job["titulo"], excluir_keywords):
            stats["no_elegibles"] += 1
            print(f"  no elegible: {job['titulo']} @ {job['empresa']} (ver excluir_titulo_keywords)")
            continue

        if not es_ubicacion_compatible(job, ciudad):
            stats["fuera_de_ciudad"] += 1
            print(f"  otra ciudad, no remota: {job['titulo']} @ {job['empresa']} (se descarta sin gastar Gemini)")
            continue

        # Un fallo con UNA oferta (Gemini caído, Playwright roto, la API del dashboard sin
        # responder, etc.) no debe tumbar el resto del ciclo.
        try:
            score_result = score_job(job, gemini_key, cv_text=cv_text, modalidades=modalidades, ciudad=ciudad)
            time.sleep(GEMINI_SLEEP_SECONDS)

            if score_result["score"] < score_threshold:
                stats["descartadas"] += 1
                print(f"  [{score_result['score']}] {job['titulo']} @ {job['empresa']} (descartada por score)")
                continue

            cv_path = None
            try:
                cv_path = generate_tailored_cv(job, gemini_key, cv_text=cv_text)
                time.sleep(GEMINI_SLEEP_SECONDS)
            except Exception as e:
                print(f"[main] error generando CV adaptado para '{job['titulo']}': {e}")

            cred = credenciales[job["fuente"]]
            apply_result = APPLY_FUNCTIONS[job["fuente"]](
                job, auto_apply_config, email=cred["email"], password=cred["password"]
            )

            if apply_result["success"]:
                estado = "Aplicado"
            else:
                estado = "Descartado"
                score_result = {
                    **score_result,
                    "justificacion": f"{score_result['justificacion']} | No se pudo auto-aplicar: {apply_result['reason']}",
                }

            report_job(api_base, api_token, job, score_result, estado, cv_path, apply_reason=apply_result.get("reason"))
            existing_ids.add(job["id_externo"])

            if estado == "Aplicado":
                stats["auto_aplicadas"] += 1
                print(f"  [{score_result['score']}] {job['titulo']} @ {job['empresa']} -> Aplicado")
            else:
                stats["no_auto_aplicables"] += 1
                print(f"  [{score_result['score']}] {job['titulo']} @ {job['empresa']} -> no auto-aplicable: {apply_result['reason']}")
        except Exception as e:
            stats["errores"] += 1
            print(f"[main] error inesperado procesando '{job['titulo']}' @ {job['empresa']}: {e}")

    print(
        f"\n[main] Resumen: {stats['auto_aplicadas']} auto-aplicadas | "
        f"{stats['no_auto_aplicables']} no auto-aplicables | "
        f"{stats['descartadas']} descartadas por score bajo | {stats['no_elegibles']} no elegibles | "
        f"{stats['fuera_de_ciudad']} de otra ciudad (no remotas) | "
        f"{stats['ya_procesadas']} ya procesadas | {stats['errores']} con error inesperado | "
        f"{stats['sin_tiempo']} sin tiempo (quedan para el próximo ciclo)"
    )


if __name__ == "__main__":
    run()
