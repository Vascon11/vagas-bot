from datetime import date
from pathlib import Path

import pandas as pd

from hermes.db import Database
from hermes.search import load_queries, run_searches
from hermes.sources.base import SearchQuery
from hermes.sources.jobspy_source import JobSpySource, row_to_posting

# Formato real devolvido pelo JobSpy 1.1.82 (busca de 29/09/2026), reduzido às
# colunas que o Hermes usa. Inclui os NaN que ele manda em campo vazio.
ROWS = [
    {"id": "li-4470895064", "site": "linkedin", "title": "Desenvolvedor FullStack Java/React Júnior",
     "company": "Nava | Tech for Business", "location": "São Paulo, São Paulo, Brazil",
     "date_posted": date(2026, 9, 28), "job_url": "https://www.linkedin.com/jobs/view/4470895064",
     "description": "Descrição…", "job_level": "mid-senior level", "is_remote": False},
    {"id": "li-4471202883", "site": "linkedin", "title": "Programa de Estágio Motorola Solutions 2027",
     "company": "Motorola Solutions", "location": float("nan"), "date_posted": float("nan"),
     "job_url": "https://www.linkedin.com/jobs/view/4471202883", "description": float("nan"),
     "job_level": "internship", "is_remote": float("nan")},
    {"id": "li-0", "site": "linkedin", "title": float("nan"), "company": "X", "location": None,
     "date_posted": None, "job_url": "https://x", "description": None, "job_level": None,
     "is_remote": None},
]


def fake_scrape(rows):
    calls = []

    def scrape(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame(rows)

    scrape.calls = calls
    return scrape


def test_row_to_posting_cleans_nan():
    p = row_to_posting(ROWS[1])
    assert p.external_id == "li-4471202883"
    assert p.location is None and p.posted_at is None and p.description is None
    assert p.is_remote is None


def test_row_without_title_is_skipped():
    assert row_to_posting(ROWS[2]) is None


def test_jobspy_source_maps_rows_and_passes_query():
    scrape = fake_scrape(ROWS)
    q = SearchQuery(name="t", term="estágio", sites=("linkedin", "gupy"), hours_old=24)
    postings = JobSpySource(scrape).search(q)
    assert [p.external_id for p in postings] == ["li-4470895064", "li-4471202883"]
    assert postings[0].posted_at == date(2026, 9, 28)
    # "gupy" não é do JobSpy e não pode ser repassado a ele
    assert scrape.calls[0]["site_name"] == ["linkedin"]
    assert scrape.calls[0]["hours_old"] == 24


def test_run_searches_dedups_and_survives_failing_source():
    class Broken:
        def search(self, q):
            raise RuntimeError("HTTP 429")

    db = Database(":memory:")
    queries = [SearchQuery(name="a", term="a"), SearchQuery(name="b", term="b")]
    result = run_searches(queries, [JobSpySource(fake_scrape(ROWS)), Broken()], db, sleep=lambda s: None)
    assert len(result.found) == 2  # a segunda busca traz as mesmas vagas
    assert len(result.new_ids) == 2
    assert len(result.errors) == 2 and "429" in result.errors[0]


def test_dry_run_writes_nothing():
    result = run_searches([SearchQuery(name="a", term="a")], [JobSpySource(fake_scrape(ROWS))], None)
    assert len(result.found) == 2 and result.new_ids == []


def test_searches_yaml_loads():
    queries = load_queries(Path(__file__).parent.parent / "searches.yaml")
    assert queries and all("linkedin" in q.sites for q in queries)
    assert {q.name for q in queries} >= {"estagio-dev"}
