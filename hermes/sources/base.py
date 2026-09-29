from dataclasses import dataclass, field
from typing import Protocol

from hermes.models import JobPosting


@dataclass(frozen=True)
class SearchQuery:
    """Uma busca do searches.yaml. Cada fonte usa os campos que entende."""

    name: str
    term: str
    location: str = "São Paulo, Brasil"
    sites: tuple[str, ...] = ("linkedin",)
    hours_old: int = 48
    job_type: str | None = None  # fulltime, parttime, internship, contract
    is_remote: bool = False
    results_wanted: int = 15
    extra: dict = field(default_factory=dict)


class Source(Protocol):
    """Uma fonte de vagas. Trocar de fornecedor é trocar esta implementação."""

    def search(self, query: SearchQuery) -> list[JobPosting]: ...
