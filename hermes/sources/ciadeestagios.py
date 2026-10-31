"""Companhia de Estágios: os programas públicos do site institucional.

As vagas comuns ficam atrás de login, em app.ciadeestagios.com.br, e não entram
aqui. Os programas (/vagas/<empresa>/ e /programas/<empresa>) são públicos: o sitemap
lista as páginas e cada uma traz um JobPosting em JSON-LD, conferido em 31/10/2026.
Página sem JSON-LD é ignorada. O site não tem busca, então `term` não é usado: o
filtro de tecnologia e o de estado ficam aqui. A data de publicação é só o dia, por
isso a janela desta fonte (`hours_old` no searches.yaml) precisa ser maior.
"""

import html
import json
import logging
import re
import time
from datetime import UTC, date, datetime, timedelta

import httpx

from hermes.models import JobPosting
from hermes.sources.base import SearchQuery

log = logging.getLogger(__name__)

SITEMAP = "https://www.ciadeestagios.com.br/sitemap-programas-de-estagio.xml"
HEADERS = {"User-Agent": "Mozilla/5.0"}
PAUSE = 1.0  # segundos entre páginas: é um site pequeno

SITEMAP_ENTRY = re.compile(r"<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>")
JSON_LD = re.compile(r"<script[^>]*ld\+json[^>]*>(.*?)</script>", re.DOTALL)
TITLE_SUFFIX = re.compile(r"\s*\|\s*Companhia de Estágios\s*$")
# Cursos de tecnologia citados na página. Sem "TI" solto em minúsculas: "ti" é palavra.
TECH = re.compile(
    r"computa[cç][aã]o|sistemas de informa[cç][aã]o|ci[eê]ncia de dados"
    r"|tecnologia da informa[cç][aã]o|engenharia de software|an[aá]lise e desenvolvimento"
    r"|\bsoftware\b",
    re.IGNORECASE,
)


def parse_sitemap(xml: str) -> list[tuple[str, datetime]]:
    return [(url, datetime.fromisoformat(mod)) for url, mod in SITEMAP_ENTRY.findall(xml)]


def find_job_posting(page: str) -> dict | None:
    for block in JSON_LD.findall(page):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for node in nodes:
            if isinstance(node, dict) and node.get("@type") == "JobPosting":
                return node
    return None


def is_tech(page: str) -> bool:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.DOTALL)
    return bool(TECH.search(html.unescape(re.sub(r"<[^>]+>", " ", text))))


def places(job: dict) -> list[tuple[str | None, str | None]]:
    """(cidade, UF) de cada local. O JSON-LD traz o endereço ou só "Cidade, UF" no nome."""
    raw = job.get("jobLocation") or []
    found = []
    for place in raw if isinstance(raw, list) else [raw]:
        addr = place.get("address") or {}
        city, state = addr.get("addressLocality"), addr.get("addressRegion")
        if not state and "," in (place.get("name") or ""):
            city, state = (s.strip() for s in place["name"].rsplit(",", 1))
        found.append((city, state))
    return found


def job_to_posting(job: dict, url: str, state: str | None) -> JobPosting | None:
    """Sem local informado a vaga passa; com local, um deles precisa ser no estado."""
    title = TITLE_SUFFIX.sub("", job.get("title") or "").strip()
    company = (job.get("hiringOrganization") or {}).get("name")
    if not (title and company):
        return None
    where = places(job)
    in_state = [p for p in where if not state or p[1] == state]
    if where and not in_state:
        return None
    return JobPosting(
        source="ciadeestagios",
        external_id=url.rstrip("/").rsplit("/", 1)[-1],
        title=html.unescape(title),
        company=company,
        url=url,
        location=", ".join(p for p in (in_state or [(None, None)])[0] if p) or None,
        description=job.get("description"),
        posted_at=date.fromisoformat(job["datePosted"][:10]),
        level="internship" if job.get("employmentType") == "INTERN" else None,
    )


class CiaDeEstagiosSource:
    def __init__(self, client: httpx.Client | None = None, now=None, sleep=time.sleep):
        self._client = client or httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True)
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep

    def search(self, query: SearchQuery) -> list[JobPosting]:
        if "ciadeestagios" not in query.sites:
            return []
        now = self._now()
        cutoff = now - timedelta(hours=query.hours_old)
        resp = self._client.get(SITEMAP)
        resp.raise_for_status()
        # A página muda quando o programa é publicado: só as alteradas na janela são lidas.
        urls = [url for url, mod in parse_sitemap(resp.text) if mod >= cutoff]
        state = query.extra.get("cia_state", "SP")
        postings = []
        for url in urls:
            self._sleep(PAUSE)
            page = self._client.get(url)
            if page.status_code == 404:  # o sitemap lista programas já retirados
                continue
            page.raise_for_status()
            job = find_job_posting(page.text)
            if not job or not job.get("datePosted") or not is_tech(page.text):
                continue
            # Edição em programa antigo também muda o lastmod; encerrado não interessa.
            if job["datePosted"][:10] < cutoff.date().isoformat():
                continue
            if job.get("validThrough") and job["validThrough"][:10] < now.date().isoformat():
                continue
            if p := job_to_posting(job, url, state):
                postings.append(p)
        log.info("%s: %d programas da ciadeestagios", query.name, len(postings))
        return postings[: query.results_wanted]
