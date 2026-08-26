"""Fuentes de ofertas que requieren un navegador headless (Playwright) porque su contenido
se renderiza con JavaScript y no aparece en el HTML crudo de una petición normal.

Cada función abre y cierra su propio navegador (más lento que fetch_jobs.py, pero estas
páginas no dan otra opción). Devuelven None si algo falla o la página cambió de estructura,
igual que las fuentes de fetch_jobs.py, para poder avisar por Telegram."""

import time

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 job-hunter-ai"
)


def _scrape_with_retry(scrape_fn, source, context, retries=2, delay_seconds=8):
    """Reintenta `scrape_fn` ante timeouts transitorios (páginas lentas, un solo request que se
    cuelga) antes de reportar la fuente como caída -- un timeout suelto en una sola búsqueda no
    debería disparar la alerta de "fuente rota" si en el siguiente intento carga bien."""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return scrape_fn()
        except Exception as e:
            last_error = e
            if attempt < retries:
                print(f"[{source}] intento {attempt}/{retries} falló {context} ({e}), reintentando en {delay_seconds}s...")
                time.sleep(delay_seconds)
    print(f"[{source}] error {context} tras {retries} intentos: {last_error}")
    return None


def fetch_getonbrd(category="programming", limit=40):
    """Scrapea el listado de GetOnBrd para una categoría (bolsa de empleo tech de LatAm,
    incluye Colombia). robots.txt de getonbrd.com permite bots genéricos (solo bloquea
    crawlers de entrenamiento de IA por nombre)."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(f"https://www.getonbrd.com/jobs/{category}", timeout=30000, wait_until="domcontentloaded")
            page.wait_for_selector(".results-item", timeout=15000)
            items = page.eval_on_selector_all(
                ".results-item",
                """
                els => els.map(el => ({
                    url: el.href,
                    title: el.querySelector('.results-list-title strong')?.innerText?.trim() || '',
                    company: el.querySelector('.results-list-info > div.size0 > strong')?.innerText?.trim() || '',
                    location: el.querySelector('.location')?.innerText?.replace(/\\s+/g, ' ').trim() || '',
                }))
                """,
            )
            browser.close()
    except Exception as e:
        print(f"[getonbrd] error scrapeando '{category}': {e}")
        return None

    if not items:
        print(f"[getonbrd] 0 ofertas extraídas para '{category}' (¿cambió el HTML del sitio?)")
        return None

    seen = set()
    normalized = []
    for it in items:
        url = it["url"].split("?")[0]
        if not url or url in seen or not it["title"]:
            continue
        seen.add(url)
        job_id = url.rstrip("/").rsplit("/", 1)[-1]
        normalized.append(
            {
                "id_externo": f"getonbrd-{job_id}",
                "titulo": it["title"],
                "empresa": it["company"] or "N/A",
                "url": url,
                "descripcion": f"{it['title']} — {it['company']} — {it['location']}",
                "ubicacion": it["location"] or "LatAm",
                "fuente": "GetOnBrd",
                "fecha": "",
            }
        )
        if len(normalized) >= limit:
            break
    return normalized


def fetch_elempleo(keyword, limit=20):
    """Escribe `keyword` en el buscador real de ElEmpleo (como lo haría una persona) y
    extrae los resultados desde su atributo `data-ga4-offerdata` (JSON limpio embebido por
    oferta). robots.txt de elempleo.com no restringe estas páginas, y su buscador solo filtra
    de verdad vía JS -- por eso necesita Playwright y no un GET con query params."""
    def _do():
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto("https://www.elempleo.com/co/ofertas-empleo/", timeout=30000, wait_until="domcontentloaded")
            page.wait_for_selector("#searchBox", timeout=15000)
            page.fill("#searchBox", keyword)
            page.keyboard.press("Enter")
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            try:
                page.wait_for_selector(".result-item", timeout=15000)
            except PlaywrightTimeoutError:
                pass  # puede ser 0 resultados genuinos para este keyword, no necesariamente roto
            items = page.eval_on_selector_all(
                ".js-area-bind[data-ga4-offerdata]",
                """
                els => els.map(el => {
                    try {
                        const data = JSON.parse(el.getAttribute('data-ga4-offerdata'));
                        return {...data, url: el.getAttribute('data-url')};
                    } catch (e) { return null; }
                }).filter(Boolean)
                """,
            )
            browser.close()
            return items

    items = _scrape_with_retry(_do, "elempleo", f"buscando '{keyword}'")
    if items is None:
        return None

    if not items:
        # La página cargó bien (si no, _scrape_with_retry ya habría devuelto None arriba) --
        # esto es probablemente 0 resultados genuinos para un keyword angosto, no la fuente rota.
        print(f"[elempleo] 0 ofertas para '{keyword}'")
        return []

    normalized = []
    for it in items[:limit]:
        job_id = it.get("id")
        if not job_id or not it.get("title"):
            continue
        normalized.append(
            {
                "id_externo": f"elempleo-{job_id}",
                "titulo": it["title"],
                "empresa": it.get("company") or "N/A",
                "url": f"https://www.elempleo.com{it['url']}" if it.get("url") else "",
                "descripcion": f"{it['title']} — {it.get('tags', '')}",
                "ubicacion": it.get("location") or "Colombia",
                "fuente": "ElEmpleo",
                "fecha": "",
            }
        )
    return normalized


def fetch_computrabajo(keyword, limit=20):
    """Busca `keyword` en Computrabajo Colombia vía su parámetro `?q=` (a diferencia de
    ElEmpleo, sí filtra resultados server-side con un GET normal, pero igual se renderiza acá
    con Playwright para extraer los datos de forma robusta desde el DOM ya cargado)."""
    query = keyword.replace(" ", "+")

    def _do():
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(
                f"https://co.computrabajo.com/ofertas-de-trabajo/?q={query}",
                timeout=30000,
                wait_until="domcontentloaded",
            )
            try:
                page.wait_for_selector("a.js-o-link", timeout=15000)
            except PlaywrightTimeoutError:
                pass  # puede ser 0 resultados genuinos para este keyword, no necesariamente roto
            items = page.eval_on_selector_all(
                "a.js-o-link",
                """
                els => els.map(el => {
                    const box = el.closest('article');
                    return {
                        url: el.href,
                        title: el.innerText?.trim() || '',
                        company: box?.querySelector('a.fc_base:not(.js-o-link)')?.innerText?.trim() || '',
                        location: box?.querySelector('p.fs16.fc_base:not(.dFlex) span.mr10')?.innerText?.trim() || '',
                    };
                })
                """,
            )
            browser.close()
            return items

    items = _scrape_with_retry(_do, "computrabajo", f"buscando '{keyword}'")
    if items is None:
        return None

    if not items:
        print(f"[computrabajo] 0 ofertas para '{keyword}'")
        return []

    seen = set()
    normalized = []
    for it in items:
        # OJO: sin el .split("#")[0], el fragmento de la URL (ej. "#lc=ListOffers-Score4-1",
        # la posición del resultado en la lista de búsqueda) quedaba pegado al final y el
        # id_externo terminaba siendo solo esa posición ("computrabajo-1") en vez del hash real
        # de la oferta -- eso rompía la deduplicación (dos ofertas distintas en la misma
        # posición de dos búsquedas distintas compartían id_externo) y probablemente explica por
        # qué nunca se auto-aplicó a ninguna oferta real de Computrabajo.
        url = it["url"].split("?")[0].split("#")[0]
        if not url or url in seen or not it["title"]:
            continue
        seen.add(url)
        job_id = url.rstrip("/").rsplit("-", 1)[-1]
        normalized.append(
            {
                "id_externo": f"computrabajo-{job_id}",
                "titulo": it["title"],
                "empresa": it["company"] or "N/A",
                "url": url,
                "descripcion": f"{it['title']} — {it['company']} — {it['location']}",
                "ubicacion": it["location"] or "Colombia",
                "fuente": "Computrabajo",
                "fecha": "",
            }
        )
        if len(normalized) >= limit:
            break
    return normalized


def fetch_trabajoscom(keyword, limit=20):
    """Busca `keyword` en Trabajos.com Colombia (IDPAIS=40) vía su propio buscador. robots.txt
    permite bots genéricos (solo bloquea Baidu/Yandex por completo), y sus condiciones de uso no
    prohíben acceso automatizado."""
    query = keyword.replace(" ", "+")

    def _do():
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(
                f"https://colombia.trabajos.com/bolsa-empleo/?CADENA={query}&IDPAIS=40&SUBMIT=Buscar+empleo",
                timeout=30000,
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(1500)
            items = page.eval_on_selector_all(
                "div.card.oferta",
                """
                els => els.map(el => ({
                    url: el.querySelector('a.oferta')?.href || '',
                    title: el.querySelector('a.oferta')?.innerText?.trim() || '',
                    company: el.querySelector('a.empresa span')?.innerText?.trim() || '',
                    location: el.querySelector('.info-oferta .location')?.innerText?.replace(/\\s+/g, ' ').trim() || '',
                }))
                """,
            )
            browser.close()
            return items

    items = _scrape_with_retry(_do, "trabajoscom", f"buscando '{keyword}'")
    if items is None:
        return None

    if not items:
        print(f"[trabajoscom] 0 ofertas para '{keyword}'")
        return []

    seen = set()
    normalized = []
    for it in items:
        url = it["url"].split("?")[0]
        if not url or url in seen or not it["title"]:
            continue
        seen.add(url)
        job_id = url.rstrip("/").rsplit("/", 2)[-2]
        normalized.append(
            {
                "id_externo": f"trabajoscom-{job_id}",
                "titulo": it["title"],
                "empresa": it["company"] or "N/A",
                "url": url,
                "descripcion": f"{it['title']} — {it['company']} — {it['location']}",
                "ubicacion": it["location"] or "Colombia",
                "fuente": "Trabajos.com",
                "fecha": "",
            }
        )
        if len(normalized) >= limit:
            break
    return normalized


def fetch_manpowergroup(keyword, limit=20):
    """Busca `keyword` en el portal de carreras de ManpowerGroup Colombia (Avature). robots.txt
    permite explícitamente el path /careers (con excepciones a un Disallow: / general), y no
    exige CAPTCHA en ningún paso del login/postulación."""

    def _do():
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(
                "https://manpowergroupco.avature.net/es_CO/careers/SearchJobs",
                timeout=30000,
                wait_until="domcontentloaded",
            )
            page.wait_for_selector("input[type=text]", timeout=15000)
            page.fill("input[type=text]", keyword)
            page.locator("button:has-text('BUSCAR'), input[type=submit]").first.click(timeout=10000)
            page.wait_for_timeout(2000)
            items = page.eval_on_selector_all(
                "article.article--result",
                """
                els => els.map(el => {
                    const a = el.querySelector('.article__header__text__title a');
                    const spans = [...el.querySelectorAll('.article__header__text__subtitle span')]
                        .map(s => s.innerText.trim());
                    const location = spans.find(t => !t.startsWith('Publicado') && !t.startsWith('ID')) || '';
                    return {
                        url: a?.href || '',
                        title: a?.innerText?.trim() || '',
                        location: location,
                    };
                })
                """,
            )
            browser.close()
            return items

    items = _scrape_with_retry(_do, "manpowergroup", f"buscando '{keyword}'")
    if items is None:
        return None

    if not items:
        print(f"[manpowergroup] 0 ofertas para '{keyword}'")
        return []

    seen = set()
    normalized = []
    for it in items:
        url = it["url"].split("?")[0]
        if not url or url in seen or not it["title"]:
            continue
        seen.add(url)
        job_id = url.rstrip("/").rsplit("/", 1)[-1]
        normalized.append(
            {
                "id_externo": f"manpowergroup-{job_id}",
                "titulo": it["title"],
                "empresa": "ManpowerGroup Colombia",
                "url": url,
                "descripcion": f"{it['title']} — {it['location']}",
                "ubicacion": it["location"] or "Colombia",
                "fuente": "ManpowerGroup",
                "fecha": "",
            }
        )
        if len(normalized) >= limit:
            break
    return normalized


def fetch_workingnomads(category="remote-development-jobs", limit=40):
    """Scrapea el listado de WorkingNomads para una categoría (remoto global, robots.txt
    completamente abierto: `Disallow:` vacío)."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(f"https://www.workingnomads.com/{category}", timeout=30000, wait_until="domcontentloaded")
            page.wait_for_selector("a.job-desktop", timeout=15000)
            items = page.eval_on_selector_all(
                "a.job-desktop",
                """
                els => els.map(el => ({
                    url: el.href,
                    title: el.querySelector('h4')?.innerText?.trim() || '',
                    company: el.querySelector('.company')?.innerText?.trim() || '',
                    location: el.querySelector('.box .fa-map-marker')?.parentElement?.querySelector('span')?.innerText?.trim() || '',
                }))
                """,
            )
            browser.close()
    except Exception as e:
        print(f"[workingnomads] error scrapeando '{category}': {e}")
        return None

    # El listado mezcla anuncios (dominios externos) con la misma clase que las ofertas reales.
    items = [it for it in items if it["url"].startswith("https://www.workingnomads.com/jobs/")]

    if not items:
        print(f"[workingnomads] 0 ofertas extraídas para '{category}' (¿cambió el HTML del sitio?)")
        return None

    seen = set()
    normalized = []
    for it in items:
        if it["url"] in seen or not it["title"]:
            continue
        seen.add(it["url"])
        job_id = it["url"].rstrip("/").rsplit("-", 1)[-1]
        normalized.append(
            {
                "id_externo": f"workingnomads-{job_id}",
                "titulo": it["title"],
                "empresa": it["company"] or "N/A",
                "url": it["url"],
                "descripcion": f"{it['title']} — {it['company']} — {it['location']}",
                "ubicacion": it["location"] or "Remote",
                "fuente": "WorkingNomads",
                "fecha": "",
            }
        )
        if len(normalized) >= limit:
            break
    return normalized
