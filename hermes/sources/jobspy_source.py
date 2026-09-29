"""LinkedIn e Indeed via JobSpy (github.com/speedyapply/JobSpy, MIT).

O JobSpy usa o endpoint público do LinkedIn, sem login — então não há conta para
ser banida. Se a biblioteca parar de funcionar, só este arquivo muda.
"""

import logging
import math
from datetime import date

from hermes.models import JobPosting
from hermes.sources.base import SearchQuery

log = logging.getLogger(__name__)

JOBSPY_SITES = {"linkedin", "indeed", "google", "glassdoor"}


def _clean(value):
    """O JobSpy devolve um DataFrame: campo vazio chega como NaN, NaT ou ""."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def row_to_posting(row: dict) -> JobPosting | None:
    title = _clean(row.get("title"))
    url = _clean(row.get("job_url"))
    ext_id = _clean(row.get("id"))
    if not (title and url and ext_id):
        return None
    posted = _clean(row.get("date_posted"))
    if posted is not None and not isinstance(posted, date):
        posted = date.fromisoformat(str(posted)[:10])
    remote = _clean(row.get("is_remote"))
    return JobPosting(
        source=str(row["site"]),
        external_id=str(ext_id),
        title=str(title).strip(),
        company=str(_clean(row.get("company")) or "Empresa não informada").strip(),
        url=str(url),
        location=_clean(row.get("location")),
        description=_clean(row.get("description")),
        posted_at=posted,
        level=_clean(row.get("job_level")),
        is_remote=bool(remote) if remote is not None else None,
    )


class JobSpySource:
    def __init__(self, scrape=None):
        # Injetável para os testes não irem à rede.
        if scrape is None:
            from jobspy import scrape_jobs as scrape
        self._scrape = scrape

    def search(self, query: SearchQuery) -> list[JobPosting]:
        sites = [s for s in query.sites if s in JOBSPY_SITES]
        if not sites:
            return []
        df = self._scrape(
            site_name=sites,
            search_term=query.term,
            location=query.location,
            country_indeed="brazil",
            hours_old=query.hours_old,
            job_type=query.job_type,
            is_remote=query.is_remote,
            results_wanted=query.results_wanted,
            linkedin_fetch_description=True,  # a nota da fase 2 precisa da descrição
            verbose=0,
        )
        postings = [p for r in df.to_dict("records") if (p := row_to_posting(r))]
        log.info("%s: %d vagas de %s", query.name, len(postings), ", ".join(sites))
        return postings
