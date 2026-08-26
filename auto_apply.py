"""Auto-aplica en ElEmpleo (la única fuente con un flujo nativo de aplicación, sin ATS externo,
sin CAPTCHA ni preguntas EEOC — ver justificación en README). Si la oferta trae preguntas que no
se pueden responder con certeza desde el CV/config, se detiene ANTES de enviar nada y la deja
para aplicación manual, en vez de adivinar.
"""

import os
import re
import unicodedata

from playwright.sync_api import sync_playwright

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 job-hunter-ai"
)


def _normalize(text):
    text = (text or "").lower()
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def _save_screenshot(page, id_externo):
    """Guarda un screenshot final como comprobante. Nunca lanza -- un fallo acá no debe
    afectar el resultado ya determinado de la aplicación."""
    try:
        path = os.path.join(os.path.dirname(__file__), "generated_cvs", f"apply-{id_externo}.png")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        page.screenshot(path=path)
    except Exception as e:
        print(f"[auto_apply] no se pudo guardar el screenshot (no afecta el resultado): {e}")


def _fill_carta_presentacion(page, texto):
    """Rellena el paso OPCIONAL de "Carta de presentación" que Trabajos.com muestra después de
    confirmar la postulación. Nunca lanza -- la postulación ya se envió antes de este paso, así
    que un fallo acá no debe afectar el resultado ya determinado."""
    if not texto:
        return
    try:
        textarea = page.locator("textarea:visible")
        if textarea.count() == 0:
            return
        textarea.first.fill(texto)
        submit = page.locator(
            "button:has-text('Guardar'), button:has-text('Enviar'), input[type=submit]:visible"
        )
        if submit.count() > 0:
            submit.first.click(timeout=5000)
            page.wait_for_timeout(1000)
    except Exception as e:
        print(f"[auto_apply] no se pudo rellenar la carta de presentación (no afecta el resultado): {e}")


def _match_answer(question_text, answers):
    """Devuelve la respuesta configurada si la pregunta calza con un patrón conocido, o None si
    no hay forma segura de responderla (en cuyo caso la oferta se deja para aplicación manual)."""
    q = _normalize(question_text)
    if "salari" in q:
        return answers.get("salario")
    if any(k in q for k in ["nivel academic", "academico", "escolaridad", "nivel de estudio", "formacion"]):
        return answers.get("academico")
    if "donde vive" in q or "ciudad de residencia" in q or q.strip().startswith("ciudad"):
        return answers.get("ubicacion")
    if "nivel de ingles" in q or re.search(r"\bingles\b", q):
        return answers.get("ingles")
    if "certificacion" in q or "certificado" in q:
        return answers.get("certificacion")

    if "experiencia" in q:
        # Se revisa primero "no relacionadas": si la pregunta nombra una plataforma que NO
        # maneja (ej. "Azure SQL... arquitectura Serverless"), eso pesa más que una palabra
        # generica como "serverless" que también aparece en su stack real (AWS) -- evita
        # exagerar experiencia con la plataforma equivocada solo por una palabra en común.
        no_relacionadas = answers.get("experiencia_tech_no_relacionadas", [])
        if any(_normalize(tech) in q for tech in no_relacionadas):
            return answers.get("experiencia_tech_sin_experiencia")

        conocidas = answers.get("experiencia_tech_conocidas", [])
        if any(_normalize(tech) in q for tech in conocidas):
            # Tecnología real de su stack -> respuesta honesta, con tope (nunca exagerar).
            return answers.get("experiencia_tech_respuesta")

        # No nombra ninguna tecnología reconocible (pregunta por el rol/cargo en general).
        return answers.get("experiencia_general")

    return None


def apply_to_elempleo(job, config, email=None, password=None):
    """Intenta postular automáticamente a `job` (debe ser fuente='ElEmpleo'). Devuelve
    {"success": bool, "reason": str}. Nunca envía nada si encuentra una pregunta que no puede
    responder con certeza -- se detiene y deja la oferta para aplicación manual."""
    email = email or os.environ.get("ELEMPLEO_EMAIL")
    password = password or os.environ.get("ELEMPLEO_PASSWORD")
    if not email or not password:
        return {"success": False, "reason": "faltan ELEMPLEO_EMAIL/ELEMPLEO_PASSWORD"}

    answers = config.get("auto_apply_answers", {})
    api_results = []  # [(url, status), ...] de las respuestas a /JobOffers/.../Save o similar

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})
            page.on(
                "response",
                lambda resp: api_results.append((resp.url, resp.status))
                if "/JobOffers/" in resp.url and resp.request.method == "POST"
                else None,
            )
            page.goto(job["url"], timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)

            apply_btn = page.locator(".js-login-extra-params.aplicar-oferta-detalle:visible")
            if apply_btn.count() == 0:
                browser.close()
                return {"success": False, "reason": "no se encontró el botón 'Postularme a oferta'"}
            apply_btn.first.click(timeout=10000)
            page.wait_for_timeout(1200)

            consent = page.locator("a.close-habeas")
            if consent.count() > 0:
                consent.first.click(timeout=5000)
                page.wait_for_timeout(1000)

            # Algunas ofertas (según el empleador) redirigen a "registro-rapido" en vez de
            # "iniciar-sesion" -- un formulario que pide tipo y número de identificación (cédula),
            # un dato que no se le pide al usuario en el registro. No hay forma segura de
            # inventarlo, así que se detiene acá con un motivo claro en vez del genérico de abajo.
            if "registro-rapido" in page.url:
                browser.close()
                return {
                    "success": False,
                    "reason": "esta oferta pide completar un registro rápido con número de "
                    "cédula, un dato que no tenemos guardado -- hay que aplicar manualmente",
                }

            if "iniciar-sesion" in page.url:
                # El banner de cookies de esta página tapa la parte de abajo -- no bloquea el
                # botón de enviar, pero se cierra igual por si acaso interfiere con algo.
                cookie_banner = page.locator("button:has-text('Entiendo')")
                if cookie_banner.count() > 0:
                    cookie_banner.first.click(timeout=3000)
                    page.wait_for_timeout(500)

                page.fill("input[type=email], input[placeholder*=elempleo]", email)
                page.fill("input[placeholder='Contraseña']", password)
                page.click("button:has-text('Inicia sesión')")
                # Un timeout fijo de 2.5s no alcanzaba -- el login seguía "cargando" (spinner
                # visible en el botón) cuando se leía el resultado, y se reportaba como fallido
                # un login que en realidad sí iba a completarse un segundo después. Se espera
                # explícitamente a salir de iniciar-sesion, con un timeout más generoso como
                # respaldo si por lo que sea nunca redirige.
                try:
                    page.wait_for_url(lambda url: "iniciar-sesion" not in url, timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)

            if "Questionnaires" in page.url:
                boxes = page.locator("textarea").all()
                pending = []
                for box in boxes:
                    label = box.evaluate(
                        "el => el.closest('div')?.previousElementSibling?.innerText || ''"
                    )
                    answer = _match_answer(label, answers)
                    if answer is None:
                        browser.close()
                        return {
                            "success": False,
                            "reason": f"pregunta sin respuesta segura configurada: {label.strip()[:150]}",
                        }
                    pending.append((box, answer))

                for box, answer in pending:
                    box.fill(answer)

                submit = page.locator("button[type=submit]:has-text('Postularme a oferta'):visible")
                if submit.count() == 0:
                    browser.close()
                    return {"success": False, "reason": "no se encontró el botón para enviar el cuestionario"}
                submit.first.click(timeout=10000)
                page.wait_for_timeout(2000)

            skip_note = page.locator("text=Omitir este paso")
            if skip_note.count() > 0:
                # Paso opcional -- si el clic falla (ej. quedó no-visible en este flujo puntual),
                # no debe perderse el resultado real de si ya se aplicó o no. Pasó en producción:
                # un timeout acá tumbaba el intento entero con "error inesperado" antes de
                # siquiera llegar a leer el texto de confirmación de la página.
                try:
                    skip_note.first.click(timeout=5000)
                    page.wait_for_timeout(1500)
                except Exception as e:
                    print(f"[auto_apply] no se pudo hacer clic en 'Omitir este paso' (no debería afectar el resultado): {e}")

            # El resultado se decide ANTES de intentar el screenshot: si capturar la pantalla
            # falla (pasó en producción -- "Protocol error: Unable to capture screenshot"), eso
            # no debe hacer que se reporte como fallida una aplicación que sí se envió.
            page_text = page.locator("body").inner_text()
            normalized_text = _normalize(page_text)
            ok_responses = [r for r in api_results if 200 <= r[1] < 300]
            confirmed_by_text = any(
                phrase in normalized_text
                for phrase in [
                    "exitosa",
                    "gracias por postularte",
                    # confirmación real observada en producción cuando ya se había aplicado antes
                    # (la página lo dice en vez de re-enviar la postulación).
                    "ya te postulaste a esta oferta",
                    "postulacion realizada",
                ]
            )
            success = bool(ok_responses or confirmed_by_text)

            _save_screenshot(page, job["id_externo"])
            browser.close()

            if success:
                return {
                    "success": True,
                    "reason": "aplicación enviada"
                    + (f" ({len(ok_responses)} respuesta(s) OK del servidor)" if ok_responses else " (por texto de confirmación, sin respuesta de red capturada)"),
                }
            return {
                "success": False,
                "reason": "se completó el flujo pero no se detectó confirmación clara de éxito -- revisar "
                f"manualmente. Texto de la página: {page_text[:500]!r}",
            }
    except Exception as e:
        return {"success": False, "reason": f"error inesperado: {e}"}


def apply_to_trabajoscom(job, config, email=None, password=None):
    """Intenta postular automáticamente a `job` (debe ser fuente='Trabajos.com'). Su botón
    "APLICA A LA VACANTE" abre un modal de login simple (Email/Contraseña, sin CAPTCHA) que
    hace login y aplica en un solo paso, igual que Computrabajo. Si tras enviar el modal no
    aparece la confirmación esperada (ej. porque la oferta trae un formulario adicional no
    reconocido), se detiene sin inventar nada."""
    email = email or os.environ.get("TRABAJOSCOM_EMAIL")
    password = password or os.environ.get("TRABAJOSCOM_PASSWORD")
    if not email or not password:
        return {"success": False, "reason": "faltan TRABAJOSCOM_EMAIL/TRABAJOSCOM_PASSWORD"}
    answers = config.get("auto_apply_answers", {})

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})
            page.goto(job["url"], timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)

            apply_btn = page.locator("text=APLICA A LA VACANTE")
            if apply_btn.count() == 0:
                browser.close()
                return {"success": False, "reason": "no se encontró el botón 'APLICA A LA VACANTE'"}
            apply_btn.first.click(timeout=10000)
            page.wait_for_timeout(1200)

            dialog = page.locator(".ui-dialog:visible")
            if dialog.count() == 0:
                browser.close()
                return {"success": False, "reason": "no apareció el modal de login/aplicación"}

            # ojo: además del input visible id="USUARIO", el modal trae un input OCULTO con el
            # mismo name="USUARIO" (id="USUARIOHOLA", para el caso de sesión ya iniciada) -- por
            # eso se usa el id exacto y no el name, que matchearía ambos.
            email_field = dialog.locator("#USUARIO")
            password_field = dialog.locator("input[type=password]:visible")
            if email_field.count() == 0 or password_field.count() == 0:
                browser.close()
                return {
                    "success": False,
                    "reason": "el modal no tiene los campos de email/contraseña esperados -- revisar manualmente",
                }
            email_field.first.fill(email)
            password_field.first.fill(password)

            # el modal trae un segundo input[type=submit] oculto para el formulario de registro
            # (id="BOTONREGISTRO") -- se apunta al id exacto del de login para no depender del
            # orden en el DOM.
            submit = dialog.locator("#BOTONLOGIN")
            if submit.count() == 0:
                browser.close()
                return {"success": False, "reason": "no se encontró el botón 'Aplicar' dentro del modal"}
            submit.first.click(timeout=10000)
            page.wait_for_timeout(2500)

            page_text = page.locator("body").inner_text()
            normalized = _normalize(page_text)

            # Confirmado con un envío real en producción: el texto exacto es "¡Enhorabuena
            # [Nombre]! Ya te has inscrito en la oferta [Título]." -- después trae un paso
            # OPCIONAL de carta de presentación ("Rellena los siguientes datos para diferenciarte
            # del resto de candidatos"). La postulación ya queda confirmada antes de llegar a ese
            # paso, así que rellenarlo es un plus, no un requisito -- si falla no cambia el
            # resultado ya determinado.
            success = (
                "te has inscrito" in normalized
                or "has aplicado" in normalized
                or "aplicado correctamente" in normalized
            )
            if success:
                _fill_carta_presentacion(page, answers.get("carta_presentacion"))

            _save_screenshot(page, job["id_externo"])
            browser.close()

            if "contrasena incorrecta" in normalized or "clave incorrecta" in normalized:
                return {"success": False, "reason": "contraseña incorrecta -- revisar TRABAJOSCOM_PASSWORD"}

            if success:
                return {"success": True, "reason": "aplicación confirmada tras el login en el modal"}

            # No se observó en vivo el texto de confirmación real (el clasificador bloqueó probar
            # con credenciales reales) -- se incluye un fragmento de la página para poder ajustar
            # la detección con datos reales del primer envío en producción, sin re-inventar nada
            # ni marcar como éxito algo que no se confirmó.
            return {
                "success": False,
                "reason": "no se detectó la confirmación esperada tras aplicar -- puede traer un paso "
                f"adicional no reconocido, revisar manualmente. Texto de la página: {page_text[:300]!r}",
            }
    except Exception as e:
        return {"success": False, "reason": f"error inesperado: {e}"}


def apply_to_manpowergroup(job, config, email=None, password=None):
    """Intenta postular automáticamente a `job` (debe ser fuente='ManpowerGroup'). El portal de
    carreras (Avature) redirige a un login sin CAPTCHA al hacer clic en 'APLICAR'. Tras iniciar
    sesión puede pedir "Preguntas específicas sobre el empleo" -- se responden con el mismo
    patrón seguro de ElEmpleo (bail sin inventar si alguna no calza con un patrón conocido)."""
    email = email or os.environ.get("MANPOWERGROUP_EMAIL")
    password = password or os.environ.get("MANPOWERGROUP_PASSWORD")
    if not email or not password:
        return {"success": False, "reason": "faltan MANPOWERGROUP_EMAIL/MANPOWERGROUP_PASSWORD"}
    answers = config.get("auto_apply_answers", {})

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})
            page.goto(job["url"], timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(1200)

            apply_link = page.locator("text=APLICAR")
            if apply_link.count() == 0:
                browser.close()
                return {"success": False, "reason": "no se encontró el botón 'APLICAR'"}
            apply_link.first.click(timeout=10000)
            page.wait_for_timeout(1500)

            if "/Login" in page.url:
                email_field = page.locator("#tpt_loginUsername")
                password_field = page.locator("#tpt_loginPassword")
                if email_field.count() == 0 or password_field.count() == 0:
                    browser.close()
                    return {
                        "success": False,
                        "reason": "no se encontraron los campos de login esperados -- revisar manualmente",
                    }
                email_field.fill(email)
                password_field.fill(password)
                # ojo: "text=INGRESAR" también matchea el link "Ingresar" del menú de navegación
                # (aparece antes en el DOM que el botón real) -- eso hacía que .first navegara al
                # link sin enviar el formulario, dejando el login sin completarse nunca.
                page.locator("button:has-text('INGRESAR')").first.click(timeout=10000)
                page.wait_for_timeout(2500)

            # Puede traer un paso de "Preguntas específicas sobre el empleo" incluso con perfil ya
            # guardado -- misma lógica de seguridad que ElEmpleo: si alguna pregunta visible no
            # calza con un patrón conocido, se detiene sin enviar nada.
            boxes = page.locator("textarea:visible").all()
            pending = []
            for box in boxes:
                if (box.input_value() or "").strip():
                    continue  # ya viene con una respuesta guardada del perfil
                label = box.evaluate(
                    "el => el.closest('div')?.previousElementSibling?.innerText || el.getAttribute('aria-label') || ''"
                )
                answer = _match_answer(label, answers)
                if answer is None:
                    browser.close()
                    return {
                        "success": False,
                        "reason": f"pregunta sin respuesta segura configurada: {label.strip()[:150]}",
                    }
                pending.append((box, answer))

            for box, answer in pending:
                box.fill(answer)

            if pending:
                submit = page.locator(
                    "button:has-text('Enviar'), button:has-text('Confirmar'), button:has-text('Postular'), input[type=submit]:visible"
                )
                if submit.count() > 0:
                    submit.first.click(timeout=10000)
                    page.wait_for_timeout(2000)

            page_text = page.locator("body").inner_text()
            normalized = _normalize(page_text)

            success = any(
                phrase in normalized
                for phrase in [
                    "postulacion ha sido enviada",
                    "postulacion enviada",
                    "gracias por postularte",
                    "gracias por aplicar",
                    "ha aplicado exitosamente",
                    "solicitud enviada",
                    "postulacion exitosa",
                    # confirmación real observada en producción: cuando el perfil ya tenía los
                    # datos guardados, Avature aplica en automático y muestra este mensaje en vez
                    # de un "gracias por aplicar" genérico -- sin esto, aplicaciones que sí se
                    # enviaron quedaban marcadas como "sin confirmar" y terminaban descartadas.
                    "como ya nos ha brindado su informacion personal",
                ]
            )

            _save_screenshot(page, job["id_externo"])
            browser.close()

            if "contrasena incorrecta" in normalized or "usuario o contrasena" in normalized:
                return {"success": False, "reason": "contraseña incorrecta -- revisar MANPOWERGROUP_PASSWORD"}

            if success:
                return {"success": True, "reason": "aplicación confirmada tras el login"}

            # No se pudo observar en vivo el texto de confirmación real (el clasificador de Claude
            # Code bloquea probar el login con credenciales reales desde este entorno) -- se
            # incluye un fragmento de la página para poder ajustar la detección con datos reales
            # del primer envío en producción, sin inventar un resultado.
            return {
                "success": False,
                "reason": "no se detectó la confirmación esperada tras aplicar -- revisar manualmente. "
                f"Texto de la página: {page_text[:500]!r}",
            }
    except Exception as e:
        return {"success": False, "reason": f"error inesperado: {e}"}


def apply_to_computrabajo(job, config, email=None, password=None):
    """Intenta postular automáticamente a `job` (debe ser fuente='Computrabajo'). Confirmado en
    exploración manual: para ofertas sin preguntas adicionales, el login ES el paso final de
    aplicación (redirige a una página "Te aplicaste correctamente"). El registro de cuenta nueva
    en Computrabajo sí tiene reCAPTCHA, pero el login de una cuenta ya existente no -- por eso esto
    solo funciona con una cuenta creada de antemano (ver README), nunca intenta registrar una.

    Si después del login aparece cualquier cosa que no sea esa confirmación (ej. un cuestionario
    no reconocido), se detiene sin enviar nada más -- no inventa respuestas."""
    email = email or os.environ.get("COMPUTRABAJO_EMAIL")
    password = password or os.environ.get("COMPUTRABAJO_PASSWORD")
    if not email or not password:
        return {"success": False, "reason": "faltan COMPUTRABAJO_EMAIL/COMPUTRABAJO_PASSWORD"}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})
            page.goto(job["url"], timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)

            apply_btn = page.locator("button:has-text('Aplicar'), a:has-text('Aplicar')")
            if apply_btn.count() == 0:
                browser.close()
                return {"success": False, "reason": "no se encontró el botón 'Aplicar'"}
            apply_btn.first.click(timeout=10000)
            page.wait_for_timeout(1500)

            if "computrabajo.com" in page.url and "Login" in page.url:
                page.locator("input[type=email], input[type=text]").first.fill(email)
                page.locator("button:has-text('Continuar')").first.click(timeout=10000)
                page.wait_for_timeout(1500)

                pw_field = page.locator("input[type=password]")
                if pw_field.count() == 0:
                    browser.close()
                    return {
                        "success": False,
                        "reason": "no apareció el campo de contraseña tras el correo -- revisar manualmente (¿cuenta nueva o con Google?)",
                    }
                pw_field.first.fill(password)
                pw_field.first.press("Enter")
                page.wait_for_timeout(3000)

            page_text = page.locator("body").inner_text()
            normalized = _normalize(page_text)

            _save_screenshot(page, job["id_externo"])
            browser.close()

            if "contrasena incorrecta" in normalized:
                return {"success": False, "reason": "contraseña incorrecta -- revisar COMPUTRABAJO_PASSWORD"}

            if "aplicaste correctamente" in normalized:
                return {"success": True, "reason": 'aplicación confirmada ("Te aplicaste correctamente")'}

            return {
                "success": False,
                "reason": "no se detectó la confirmación esperada tras el login -- puede traer un paso "
                "adicional no reconocido (cuestionario, etc.), revisar manualmente",
            }
    except Exception as e:
        return {"success": False, "reason": f"error inesperado: {e}"}
