"""Consulta ofertas de empleo en Jooble, RemoteOK, Remotive, WeWorkRemotely y GetOnBrd
(fuentes gratuitas; GetOnBrd requiere navegador headless, ver fetch_scraped.py)."""

import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from itertools import zip_longest

import requests

from fetch_scraped import (
    fetch_computrabajo,
    fetch_elempleo,
    fetch_getonbrd,
    fetch_manpowergroup,
    fetch_trabajoscom,
    fetch_workingnomads,
)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "data", "config.json")
USER_AGENT = "Mozilla/5.0 (job-hunter-ai; +https://github.com/)"
COLOMBIA_TZ = timezone(timedelta(hours=-5))

# Remotive pide explícitamente no consultar su API gratuita más de ~4 veces al día.
# El cron corre 6 veces al día (cada 4h); se omite en 2 de esas 6 corridas para cumplirlo.
REMOTIVE_ALLOWED_HOURS = {8, 12, 16, 20}
REMOTIVE_ALLOWED_CATEGORIES = {"Software Development", "Information Technology", "Data and Analytics"}


def _strip_html(text):
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def _parse_rfc822_date(text):
    """Convierte un pubDate de RSS (ej. 'Wed, 22 Jul 2026 07:03:46 +0000') a 'YYYY-MM-DD'."""
    try:
        return parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError):
        return ""


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_jooble(api_key, keyword, location):
    """POST a la API de Jooble. Devuelve lista de ofertas normalizadas."""
    url = f"https://jooble.org/api/{api_key}"
    try:
        resp = requests.post(
            url,
            json={"keywords": keyword, "location": location},
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[jooble] error consultando '{keyword}' / '{location}': {e}")
        return []

    jobs = resp.json().get("jobs", [])
    normalized = []
    for job in jobs:
        normalized.append(
            {
                "id_externo": f"jooble-{job.get('id')}",
                "titulo": job.get("title", "").strip(),
                "empresa": job.get("company", "").strip() or "N/A",
                "url": job.get("link", ""),
                "descripcion": job.get("snippet", ""),
                "ubicacion": job.get("location", ""),
                "fuente": "Jooble",
                "fecha": job.get("updated", ""),
            }
        )
    return normalized


def fetch_remoteok(tag):
    """GET al feed JSON de RemoteOK filtrado por tag. Devuelve ofertas normalizadas."""
    url = f"https://remoteok.com/remote-{tag}-jobs.json"
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[remoteok] error consultando tag '{tag}': {e}")
        return []

    try:
        jobs = resp.json()
    except ValueError:
        return []

    normalized = []
    for job in jobs:
        # El primer elemento del feed es un aviso legal, no una oferta.
        if not isinstance(job, dict) or "id" not in job or "position" not in job:
            continue
        normalized.append(
            {
                "id_externo": f"remoteok-{job.get('id')}",
                "titulo": job.get("position", "").strip(),
                "empresa": job.get("company", "").strip() or "N/A",
                "url": job.get("url", "") or f"https://remoteok.com/l/{job.get('id')}",
                "descripcion": job.get("description", "") or ", ".join(job.get("tags", [])),
                "ubicacion": job.get("location", "Remote"),
                "fuente": "RemoteOK",
                "fecha": job.get("date", ""),
            }
        )
    return normalized


def fetch_weworkremotely(category):
    """GET al feed RSS de WeWorkRemotely para una categoría. Devuelve ofertas normalizadas,
    o None si la fuente está caída o cambió de formato (para poder avisar por Telegram)."""
    url = f"https://weworkremotely.com/categories/{category}.rss"
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[weworkremotely] error consultando '{category}': {e}")
        return None

    try:
        channel = ET.fromstring(resp.content).find("channel")
        items = channel.findall("item")
    except (ET.ParseError, AttributeError) as e:
        print(f"[weworkremotely] error parseando el RSS de '{category}' (¿cambió el formato?): {e}")
        return None

    normalized = []
    for item in items:
        title_full = (item.findtext("title") or "").strip()
        empresa, separator, titulo = title_full.partition(": ")
        if not separator:
            empresa, titulo = "N/A", title_full
        link = (item.findtext("link") or "").strip()
        normalized.append(
            {
                "id_externo": f"wwr-{link.rstrip('/').rsplit('/', 1)[-1]}",
                "titulo": titulo.strip(),
                "empresa": empresa.strip() or "N/A",
                "url": link,
                "descripcion": _strip_html(item.findtext("description"))[:2000],
                "ubicacion": (item.findtext("region") or "Remote").strip(),
                "fuente": "WeWorkRemotely",
                "fecha": _parse_rfc822_date(item.findtext("pubDate")),
            }
        )
    return normalized


def fetch_remotive():
    """GET a la API pública de Remotive, filtrada del lado nuestro a categorías de tech
    (su parámetro `category` de la API no filtra de forma confiable). Respeta el límite de
    ~4 consultas/día que pide Remotive: solo llama en horas específicas (ver
    REMOTIVE_ALLOWED_HOURS). Devuelve None si la fuente está caída o cambió de formato."""
    hora_actual = datetime.now(COLOMBIA_TZ).hour
    if hora_actual not in REMOTIVE_ALLOWED_HOURS:
        return []

    try:
        resp = requests.get(
            "https://remotive.com/api/remote-jobs", headers={"User-Agent": USER_AGENT}, timeout=20
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[remotive] error consultando la API: {e}")
        return None

    try:
        jobs = resp.json()["jobs"]
    except (ValueError, KeyError) as e:
        print(f"[remotive] error leyendo la respuesta (¿cambió el formato de la API?): {e}")
        return None

    normalized = []
    for job in jobs:
        if job.get("category") not in REMOTIVE_ALLOWED_CATEGORIES:
            continue
        normalized.append(
            {
                "id_externo": f"remotive-{job.get('id')}",
                "titulo": (job.get("title") or "").strip(),
                "empresa": (job.get("company_name") or "").strip() or "N/A",
                "url": job.get("url", ""),
                "descripcion": _strip_html(job.get("description"))[:2000],
                "ubicacion": job.get("candidate_required_location") or "Remote",
                "fuente": "Remotive",
                "fecha": job.get("publication_date", ""),
            }
        )
    return normalized


PLATFORM_FETCHERS = {
    "ElEmpleo": fetch_elempleo,
    "Computrabajo": fetch_computrabajo,
    "Trabajos.com": fetch_trabajoscom,
    "ManpowerGroup": fetch_manpowergroup,
}


def fetch_jobs_for_platforms(cargos, plataformas):
    """Modo multi-usuario: busca `cargos` (los cargos objetivo del perfil de un usuario) en cada
    una de `plataformas` (nombres de fuente, ej. ["ElEmpleo", "Computrabajo"] -- las que ese
    usuario conectó en su registro). A diferencia de fetch_all_jobs(), no depende de
    data/config.json ni recorre las fuentes de solo-búsqueda (Jooble, RemoteOK, etc.), que no
    tienen auto-apply y por eso no aplican al flujo multi-usuario.

    Devuelve (jobs, fuentes_caidas), igual que fetch_all_jobs().
    """
    fuentes_caidas = []
    source_lists = []

    for plataforma in plataformas:
        fetcher = PLATFORM_FETCHERS.get(plataforma)
        if fetcher is None:
            continue
        for cargo in cargos:
            result = fetcher(cargo)
            if result is None:
                fuentes_caidas.append(f"{plataforma} ({cargo})")
            else:
                source_lists.append(result)

    all_jobs = {}
    for group in zip_longest(*source_lists):
        for job in group:
            if job is not None:
                all_jobs.setdefault(job["id_externo"], job)

    return list(all_jobs.values()), fuentes_caidas


def fetch_all_jobs():
    """Recorre todas las fuentes configuradas, dedupe por id_externo.

    Devuelve (jobs, fuentes_caidas): fuentes_caidas es una lista de nombres de fuentes que
    fallaron o cambiaron de formato en esta corrida (para avisar por Telegram), distinto de
    una fuente que simplemente no tuvo resultados nuevos.
    """
    config = load_config()
    jooble_key = os.environ.get("JOOBLE_API_KEY")
    fuentes_caidas = []

    # Cada fuente se acumula en su propia lista y se intercalan (round-robin) antes de
    # truncar a max_jobs_per_run, para que ninguna fuente se coma todo el cupo ella sola.
    source_lists = []

    if jooble_key:
        jooble_jobs = []
        for keyword in config["jooble_keywords"]:
            for location in config["jooble_locations"]:
                jooble_jobs.extend(fetch_jooble(jooble_key, keyword, location))
        source_lists.append(jooble_jobs)
    else:
        print("[jooble] JOOBLE_API_KEY no configurada, se omite esta fuente.")

    for tag in config["remoteok_tags"]:
        source_lists.append(fetch_remoteok(tag))

    for category in config.get("weworkremotely_categories", []):
        result = fetch_weworkremotely(category)
        if result is None:
            fuentes_caidas.append(f"WeWorkRemotely ({category})")
        else:
            source_lists.append(result)

    if config.get("remotive_enabled", True):
        result = fetch_remotive()
        if result is None:
            fuentes_caidas.append("Remotive")
        else:
            source_lists.append(result)

    for category in config.get("getonbrd_categories", []):
        result = fetch_getonbrd(category)
        if result is None:
            fuentes_caidas.append(f"GetOnBrd ({category})")
        else:
            source_lists.append(result)

    for category in config.get("workingnomads_categories", []):
        result = fetch_workingnomads(category)
        if result is None:
            fuentes_caidas.append(f"WorkingNomads ({category})")
        else:
            source_lists.append(result)

    for keyword in config.get("elempleo_keywords", []):
        result = fetch_elempleo(keyword)
        if result is None:
            fuentes_caidas.append(f"ElEmpleo ({keyword})")
        else:
            source_lists.append(result)

    for keyword in config.get("computrabajo_keywords", []):
        result = fetch_computrabajo(keyword)
        if result is None:
            fuentes_caidas.append(f"Computrabajo ({keyword})")
        else:
            source_lists.append(result)

    for keyword in config.get("trabajoscom_keywords", []):
        result = fetch_trabajoscom(keyword)
        if result is None:
            fuentes_caidas.append(f"Trabajos.com ({keyword})")
        else:
            source_lists.append(result)

    for keyword in config.get("manpowergroup_keywords", []):
        result = fetch_manpowergroup(keyword)
        if result is None:
            fuentes_caidas.append(f"ManpowerGroup ({keyword})")
        else:
            source_lists.append(result)

    all_jobs = {}
    for group in zip_longest(*source_lists):
        for job in group:
            if job is not None:
                all_jobs.setdefault(job["id_externo"], job)

    jobs = list(all_jobs.values())
    max_jobs = config.get("max_jobs_per_run")
    if max_jobs:
        jobs = jobs[:max_jobs]

    print(f"[fetch_jobs] {len(jobs)} ofertas únicas encontradas.")
    return jobs, fuentes_caidas


if __name__ == "__main__":
    found_jobs, fuentes_caidas = fetch_all_jobs()
    for job in found_jobs:
        print(f"- [{job['fuente']}] {job['titulo']} @ {job['empresa']} ({job['id_externo']})")
    if fuentes_caidas:
        print(f"\nFuentes caídas/rotas: {', '.join(fuentes_caidas)}")
