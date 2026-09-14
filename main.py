"""Orquesta el flujo completo: fetch -> score -> filtro -> generate_cv -> supabase -> telegram.

Corre en modo multi-usuario si hay perfiles registrados (tabla `perfiles` en Supabase, uno por
cada persona que se registró en el dashboard); si no hay ninguno todavía, cae al modo single-user
de siempre (data/config.json + credenciales por env var), para que la instalación original siga
funcionando sin cambios mientras se migra a un usuario real.

Limitación conocida del modo multi-usuario: el formulario de registro solo pide CV, cargos,
credenciales de plataforma y chat_id de Telegram -- no pide salario esperado, nivel académico,
tecnologías conocidas, etc. `perfiles` sí tiene columnas para eso (`auto_apply_answers` jsonb,
`excluir_titulo_keywords` array) y `_perfil_config()` las usa si están cargadas, pero el registro
no las llena todavía. Sin esos datos, `_match_answer()` en auto_apply.py no puede responder con
certeza las preguntas de cuestionarios adicionales (ElEmpleo, ManpowerGroup) para ese usuario, así
que esas ofertas se saltan de forma segura (nunca se inventa una respuesta) -- Computrabajo y
Trabajos.com, que no traen cuestionario aparte (login = aplicar), sí funcionan de lleno igual.
"""

import os
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

from auto_apply import (
    apply_to_computrabajo,
    apply_to_elempleo,
    apply_to_manpowergroup,
    apply_to_trabajoscom,
)
from crypto_utils import decrypt
from fetch_jobs import fetch_all_jobs, load_config
from generate_cv import generate_tailored_cv
from notify_telegram import (
    send_auto_apply_result,
    send_github_required_reminder,
    send_source_warning,
)
from score_match import es_ubicacion_compatible, score_job
from supabase_sync import (
    count_jobs_since,
    create_job,
    is_paused,
    load_existing_ids,
    load_perfiles,
)

load_dotenv()

APPLY_FUNCTIONS = {
    "ElEmpleo": apply_to_elempleo,
    "Computrabajo": apply_to_computrabajo,
    "Trabajos.com": apply_to_trabajoscom,
    "ManpowerGroup": apply_to_manpowergroup,
}
# Prefijo de columnas en `perfiles` para cada fuente (ej. elempleo_email/elempleo_password_enc).
PLATFORM_CRED_PREFIX = {
    "ElEmpleo": "elempleo",
    "Computrabajo": "computrabajo",
    "Trabajos.com": "trabajoscom",
    "ManpowerGroup": "manpowergroup",
}
GEMINI_SLEEP_SECONDS = float(os.environ.get("GEMINI_SLEEP_SECONDS", "15"))
COLOMBIA_TZ = timezone(timedelta(hours=-5))  # America/Bogota, sin horario de verano


def _normalize(text):
    text = (text or "").lower()
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def _es_elegible(job, config):
    """Filtra por título ANTES de gastar una llamada a Gemini -- ej. ofertas de practicante/
    aprendiz SENA/pasante, para las que el candidato ya no es elegible por haber sido practicante
    antes. No depende del criterio del modelo (falló una vez: le dio score 90 a una de estas)."""
    titulo = _normalize(job["titulo"])
    excluidas = config.get("excluir_titulo_keywords", [])
    return not any(_normalize(kw) in titulo for kw in excluidas)


def _perfil_credenciales(perfil):
    """Arma {fuente: (email, password_descifrada)} para las plataformas que ese perfil conectó
    en su registro. Una fuente sin credenciales completas simplemente no aparece acá, y sus
    ofertas se saltan (ver el filtro en _run_ciclo)."""
    creds = {}
    for fuente, prefix in PLATFORM_CRED_PREFIX.items():
        email = perfil.get(f"{prefix}_email")
        password_enc = perfil.get(f"{prefix}_password_enc")
        if not email or not password_enc:
            continue
        try:
            creds[fuente] = (email, decrypt(password_enc))
        except Exception as e:
            print(f"[main] no se pudo descifrar la contraseña de {fuente} (perfil {perfil['id']}): {e}")
    return creds


def _perfil_config(perfil):
    """Config equivalente a data/config.json pero para un perfil de usuario. `auto_apply_answers`
    y `excluir_titulo_keywords` vienen del propio perfil (columnas opcionales, jsonb/array vacíos
    por defecto) -- el registro no las pide todavía, pero cualquier usuario las puede tener
    cargadas (ej. Daniel, migrado con sus valores ya afinados) y entonces sí se usan, sin
    depender del criterio de Gemini para eso. Un perfil sin nada configurado ahí simplemente se
    comporta como antes: preguntas sin respuesta segura se saltan, sin inventar nada."""
    return {
        "auto_apply_enabled": True,
        "score_threshold": 70,
        "excluir_titulo_keywords": perfil.get("excluir_titulo_keywords") or [],
        "auto_apply_answers": perfil.get("auto_apply_answers") or {},
        "modalidades": perfil.get("modalidades") or ["Remoto", "Híbrido", "Presencial"],
        "ciudad": perfil.get("ciudad"),
    }


def _run_ciclo(jobs, fuentes_caidas, gemini_key, supabase_url, supabase_key, config, cv_text,
               credenciales, telegram_token, telegram_chat_id, user_id=None, time_budget_seconds=None):
    """Cuerpo del ciclo (filtro de elegibilidad -> score -> generar CV -> auto-apply -> guardar
    -> notificar), compartido entre el modo single-user y el loop multi-usuario.

    `time_budget_seconds`: en modo multi-usuario, cada usuario nuevo se agrega al FINAL de la
    lista de `perfiles` -- si el primero (ej. muchas ofertas, login individual por oferta en
    ElEmpleo) llena el timeout del workflow completo (pasó en producción: 45 min consumidos por
    un solo usuario), los que siguen en la lista nunca llegan a procesarse, nunca, en ningún
    ciclo. Con un presupuesto de tiempo por usuario, uno lento deja de procesar ofertas nuevas a
    tiempo mientras deja campo para los demás -- no se pierde nada, esas ofertas siguen ahí para
    el próximo ciclo (siguen sin estar en existing_ids)."""
    if fuentes_caidas and telegram_token and telegram_chat_id:
        send_source_warning(telegram_token, telegram_chat_id, fuentes_caidas)

    score_threshold = config.get("score_threshold", 70)
    existing_ids = load_existing_ids(supabase_url, supabase_key, user_id=user_id)
    deadline = time.monotonic() + time_budget_seconds if time_budget_seconds else None

    ciudad = config.get("ciudad")
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

        if deadline is not None and time.monotonic() >= deadline:
            stats["sin_tiempo"] += 1
            continue

        if job["fuente"] not in credenciales:
            # Alcance actual: solo se procesan fuentes con auto-apply disponible y credenciales.
            continue

        if not _es_elegible(job, config):
            stats["no_elegibles"] += 1
            print(f"  🚫 {job['titulo']} @ {job['empresa']} (no elegible, ver excluir_titulo_keywords)")
            continue

        if not es_ubicacion_compatible(job, ciudad):
            stats["fuera_de_ciudad"] += 1
            print(f"  📍 {job['titulo']} @ {job['empresa']} (otra ciudad, no remota -- se descarta sin gastar Gemini)")
            continue

        # Todo lo que sigue puede fallar por cosas fuera de control (Gemini caído, Playwright
        # roto, un 409 de Supabase por un id_externo que ya existía de antes de multi-usuario,
        # etc.) -- un fallo con UNA oferta no debe tumbar el resto del ciclo ni, sobre todo,
        # dejar sin enviar el resumen final de Telegram (pasó en producción: un 409 a mitad de
        # ciclo mataba la corrida completa de este usuario en silencio).
        try:
            score_result = score_job(job, gemini_key, cv_text=cv_text, modalidades=config.get("modalidades"), ciudad=ciudad)
            time.sleep(GEMINI_SLEEP_SECONDS)

            if score_result["score"] < score_threshold:
                stats["descartadas"] += 1
                print(f"  ✗ [{score_result['score']}] {job['titulo']} @ {job['empresa']} (descartada por score)")
                continue

            cv_path = None
            try:
                cv_path = generate_tailored_cv(job, gemini_key, cv_text=cv_text)
                time.sleep(GEMINI_SLEEP_SECONDS)
            except Exception as e:
                print(f"[main] error generando CV adaptado para '{job['titulo']}': {e}")

            if not config.get("auto_apply_enabled"):
                apply_result = {"success": False, "reason": "auto_apply_enabled está en false"}
            else:
                email, password = credenciales[job["fuente"]]
                apply_result = APPLY_FUNCTIONS[job["fuente"]](job, config, email=email, password=password)

            if apply_result["success"]:
                estado = "Aplicado"
            else:
                estado = "Descartado"
                score_result = {
                    **score_result,
                    "justificacion": f"{score_result['justificacion']} | No se pudo auto-aplicar: {apply_result['reason']}",
                }

            # Se guarda en Supabase ANTES de notificar -- si Telegram falla (timeout, etc.), el
            # registro de una aplicación real ya enviada no se pierde. Se registra en ambos casos:
            # "Aplicado" queda como comprobante, "Descartado" evita reprocesar la misma oferta cada
            # 4h (dedup por id_externo).
            create_job(supabase_url, supabase_key, job, score_result, cv_path, estado=estado, user_id=user_id)
            existing_ids.add(job["id_externo"])

            if estado == "Aplicado":
                stats["auto_aplicadas"] += 1
                print(f"  ✓ [{score_result['score']}] {job['titulo']} @ {job['empresa']} -> Aplicado")
                if telegram_token and telegram_chat_id:
                    send_auto_apply_result(telegram_token, telegram_chat_id, job, apply_result)
            else:
                stats["no_auto_aplicables"] += 1
                print(f"  ⏭️ [{score_result['score']}] {job['titulo']} @ {job['empresa']} -> no auto-aplicable: {apply_result['reason']}")
        except Exception as e:
            stats["errores"] += 1
            print(f"[main] error inesperado procesando '{job['titulo']}' @ {job['empresa']}: {e}")

    hoy_inicio = datetime.now(COLOMBIA_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    total_hoy = count_jobs_since(
        supabase_url, supabase_key, hoy_inicio.astimezone(timezone.utc).isoformat(),
        estado="Aplicado", user_id=user_id,
    )

    print(
        f"\n[main] Resumen: {stats['auto_aplicadas']} auto-aplicadas | "
        f"{stats['no_auto_aplicables']} no auto-aplicables | "
        f"{stats['descartadas']} descartadas por score bajo | {stats['no_elegibles']} no elegibles | "
        f"{stats['fuera_de_ciudad']} de otra ciudad (no remotas) | "
        f"{stats['ya_procesadas']} ya procesadas | {stats['errores']} con error inesperado | "
        f"{stats['sin_tiempo']} sin tiempo (quedan para el próximo ciclo) | "
        f"{total_hoy} aplicadas hoy en total"
    )
    # El resumen ya NO se manda por Telegram al usuario (a pedido: le resultaba ruidoso y
    # redundante con el aviso de cada postulación enviada) -- el print de arriba se mantiene
    # intacto porque el panel de admin lo parsea (RESUMEN_REGEX en lib/github.ts) para clasificar
    # ciclos reales vs gate-skips.


def _run_single_user(gemini_key, supabase_url, supabase_key):
    """Modo de siempre: 1 config.json + credenciales por env var. Se usa mientras no haya ningún
    perfil real registrado en Supabase (ver run())."""
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if is_paused(supabase_url, supabase_key):
        print("[main] Automatización pausada desde el dashboard — se omite esta corrida.")
        return

    config = load_config()
    jobs, fuentes_caidas = fetch_all_jobs()
    # (None, None) por fuente -> cada apply_to_* cae a sus env vars (ELEMPLEO_EMAIL, etc.) como
    # ya hacía antes de que existiera el modo multi-usuario.
    credenciales = {fuente: (None, None) for fuente in APPLY_FUNCTIONS}

    _run_ciclo(
        jobs, fuentes_caidas, gemini_key, supabase_url, supabase_key, config,
        cv_text=None, credenciales=credenciales,
        telegram_token=telegram_token, telegram_chat_id=telegram_chat_id, user_id=None,
    )


def _avisar_perfiles_sin_github(perfiles):
    """Conectar GitHub es obligatorio -- el cron central ya NO procesa ofertas para nadie (evita
    que el trabajo de un usuario consuma los minutos de Actions de la cuenta de Daniel). Para un
    perfil registrado que todavía no conectó su GitHub, el único trabajo de este cron es
    recordárselo por Telegram; el dashboard también lo muestra como advertencia permanente."""
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    for perfil in perfiles:
        if perfil.get("pausado"):
            continue
        chat_id = perfil.get("telegram_chat_id")
        if telegram_token and chat_id:
            send_github_required_reminder(telegram_token, chat_id)
        print(f"[main] Usuario {perfil['id']} sin GitHub conectado -- se le avisó, no se procesa acá.")


def run():
    gemini_key = os.environ["GEMINI_API_KEY"]
    supabase_url = os.environ["SUPABASE_URL"].rstrip("/")
    supabase_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

    perfiles_todos = load_perfiles(supabase_url, supabase_key)
    if not perfiles_todos:
        print("[main] No hay perfiles registrados todavía -- modo single-user (config.json + env vars).")
        _run_single_user(gemini_key, supabase_url, supabase_key)
        return

    # Conectar GitHub es obligatorio: el cron central ya NO procesa ofertas para ningún perfil,
    # conectado o no -- solo le avisa a quien le falte conectar. Cada usuario real corre en su
    # propio repo con sus propios 45 min gratis; nadie más comparte ni gasta los minutos de
    # Actions de la cuenta de Daniel.
    conectados = [p for p in perfiles_todos if p.get("github_repo_url")]
    sin_conectar = [p for p in perfiles_todos if not p.get("github_repo_url")]
    if conectados:
        print(f"[main] {len(conectados)} perfil(es) con su propio GitHub conectado -- se omiten del cron central.")
    if sin_conectar:
        _avisar_perfiles_sin_github(sin_conectar)


if __name__ == "__main__":
    run()
