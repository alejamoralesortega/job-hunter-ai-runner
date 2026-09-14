"""Envía notificaciones por Telegram sobre el progreso de la automatización."""

import requests


def _send(bot_token, chat_id, text, disable_preview=True):
    """POST a la API de Telegram. Nunca lanza -- un timeout o error de red acá no debe tumbar
    la corrida completa ni hacer que se pierda un registro que ya se guardó en Supabase.

    Texto plano, sin parse_mode: un título de oferta o una URL con un solo "_" (ej. las de
    ManpowerGroup, que traen "/es_CO/") rompe el modo "Markdown" clásico de Telegram (no soporta
    escapar caracteres, a diferencia de MarkdownV2) y el mensaje entero se pierde en silencio --
    pasó en producción con una postulación real ya confirmada. El negrito no vale ese riesgo."""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": disable_preview,
            },
            timeout=20,
        )
        if not resp.ok:
            print(f"[notify_telegram] error enviando mensaje: {resp.status_code} {resp.text}")
        return resp.ok
    except requests.RequestException as e:
        print(f"[notify_telegram] error de red enviando a Telegram (se ignora, no crashea la corrida): {e}")
        return False


def send_source_warning(bot_token, chat_id, fuentes_caidas):
    """Avisa si alguna fuente de ofertas falló o parece haber cambiado de formato en esta corrida."""
    text = (
        "⚠️ Una fuente de ofertas dejó de funcionar\n\n"
        + "\n".join(f"- {f}" for f in fuentes_caidas)
        + "\n\nProbablemente cambiaron el HTML/formato de la página. Avísale a Claude para "
        "que la revise y la arregle — mientras tanto el resto de fuentes sigue corriendo normal."
    )
    return _send(bot_token, chat_id, text)


def send_github_required_reminder(bot_token, chat_id):
    """Avisa a un usuario registrado que todavía no conectó su GitHub que su automatización no
    está corriendo -- conectar GitHub es obligatorio, el cron central ya no procesa a nadie sin
    su propio repo conectado."""
    text = (
        "⚠️ Tu automatización no está corriendo\n\n"
        "Conecta tu cuenta de GitHub desde Ajustes en el dashboard para activarla -- es gratis "
        "y toma un minuto: pega tu API key de Gemini y dale click a \"Conectar GitHub\"."
    )
    return _send(bot_token, chat_id, text)


def send_auto_apply_result(bot_token, chat_id, job, result):
    """Avisa de inmediato cada vez que el sistema envía una postulación automática exitosa."""
    text = (
        f"🚀 Postulación automática enviada\n\n"
        f"{job['titulo']}\n{job['empresa']}\n\n"
        f"{result['reason']}\n\n{job['url']}"
    )
    return _send(bot_token, chat_id, text)
