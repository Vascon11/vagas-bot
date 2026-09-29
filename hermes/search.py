import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from hermes.db import Database
from hermes.models import JobPosting
from hermes.sources.base import SearchQuery, Source

log = logging.getLogger(__name__)


def load_queries(path: Path) -> list[SearchQuery]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = data.get("defaults", {})
    queries = []
    for item in data.get("searches", []):
        merged = {**defaults, **item}
        if "sites" in merged:
            merged["sites"] = tuple(merged["sites"])
        queries.append(SearchQuery(**merged))
    return queries


@dataclass
class RunResult:
    found: list[JobPosting] = field(default_factory=list)
    new_ids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def run_searches(
    queries: list[SearchQuery],
    sources: list[Source],
    db: Database | None,
    pause: tuple[float, float] = (0, 0),
    sleep=time.sleep,
) -> RunResult:
    """Roda cada busca em cada fonte. Com db=None é dry-run: não grava nada.

    Uma fonte que falha não derruba as outras — o erro vai para o resultado.
    """
    result = RunResult()
    seen: set[str] = set()
    first = True
    for query in queries:
        for source in sources:
            if not first:
                sleep(random.uniform(*pause))
            first = False
            try:
                postings = source.search(query)
            except Exception as exc:  # rede, 429, mudança de HTML…
                log.exception("busca %r falhou em %s", query.name, type(source).__name__)
                result.errors.append(f"{query.name} / {type(source).__name__}: {exc}")
                continue
            for p in postings:
                # dedup dentro da própria rodada, que vale também no dry-run
                if p.dedup_key in seen:
                    continue
                seen.add(p.dedup_key)
                result.found.append(p)
                if db is not None and (job_id := db.add_job(p)) is not None:
                    result.new_ids.append(job_id)
    return result
