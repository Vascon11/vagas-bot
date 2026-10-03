"""Gupy via API pública do portal de vagas (a mesma que portal.gupy.io usa).

Endpoint conferido em 02/10/2026: é o proxy que o próprio portal chama no navegador.
Os anteriores (portal.api.gupy.io/api/job e employability-portal.gupy.io/api/v1/jobs)
passaram a responder 404, em 29/09 e 30/09.
Não há filtro por data na API: a janela de hours_old é aplicada aqui. O
pagination.total não é confiável, então a paginação para quando uma página vem
menor que o limite.
"""

import logging
from datetime import UTC, datetime, timedelta

import httpx

from hermes.models import JobPosting
from hermes.sources.base import SearchQuery

log = logging.getLogger(__name__)

API = "https://portal.gupy.io/api/job-search/jobs"
PAGE = 100
HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://portal.gupy.io"}

# "type" da Gupy -> nível no vocabulário do JobSpy
LEVELS = {
    "vacancy_type_internship": "internship",
    "vacancy_type_apprentice": "apprentice",
    "vacancy_type_trainee": "trainee",
    "vacancy_type_talent_pool": "talent_pool",
}


def parse_published(value: str) -> datetime:
    # Vem como "2026-05-25T19:47:17.333Z" ou só "2026-09-29", sem fuso.
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def item_to_posting(item: dict) -> JobPosting | None:
    if not (item.get("id") and item.get("name") and item.get("jobUrl")):
        return None
    place = ", ".join(p for p in (item.get("city"), item.get("state")) if p) or None
    published = item.get("publishedDate")
    return JobPosting(
        source="gupy",
        external_id=str(item["id"]),
        title=item["name"].strip(),
        company=(item.get("careerPageName") or "Empresa não informada").strip(),
        url=item["jobUrl"],
        location=place,
        description=item.get("description") or None,
        posted_at=parse_published(published).date() if published else None,
        level=LEVELS.get(item.get("type"), item.get("type")),
        is_remote=bool(item.get("isRemoteWork")) or item.get("workplaceType") == "remote",
    )


class GupySource:
    def __init__(self, client: httpx.Client | None = None, max_pages: int = 3, now=None):
        self._client = client or httpx.Client(headers=HEADERS, timeout=20)
        self._max_pages = max_pages
        self._now = now or (lambda: datetime.now(UTC))

    def search(self, query: SearchQuery) -> list[JobPosting]:
        if "gupy" not in query.sites:
            return []
        params = {"jobName": query.extra.get("gupy_term", query.term), "limit": PAGE}
        if state := query.extra.get("gupy_state", "São Paulo"):
            params["state"] = state
        cutoff = self._now() - timedelta(hours=query.hours_old)
        postings = []
        for page in range(self._max_pages):
            resp = self._client.get(API, params={**params, "offset": page * PAGE})
            resp.raise_for_status()
            data = resp.json().get("data", [])
            for item in data:
                published = item.get("publishedDate")
                if published and parse_published(published) < cutoff:
                    continue
                if (p := item_to_posting(item)) and (not query.is_remote or p.is_remote):
                    postings.append(p)
            if len(data) < PAGE:
                break
        log.info("%s: %d vagas da gupy", query.name, len(postings))
        return postings[: query.results_wanted]
