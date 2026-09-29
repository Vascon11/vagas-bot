import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Status(StrEnum):
    NOVA = "nova"
    AVALIADA = "avaliada"
    AGUARDANDO_VOCE = "aguardando_voce"
    APROVADA = "aprovada"
    APLICADA = "aplicada"
    EM_PROCESSO = "em_processo"  # questionário, teste ou entrevista
    CONTRATADO = "contratado"
    RECUSADA = "recusada"
    DESCARTADA = "descartada"
    SEM_RESPOSTA = "sem_resposta"  # 90 dias em "aplicada" sem nenhum evento


# Transições permitidas. Qualquer outra é bug e o db recusa.
TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.NOVA: frozenset({Status.AVALIADA, Status.DESCARTADA}),
    Status.AVALIADA: frozenset({Status.AGUARDANDO_VOCE, Status.DESCARTADA}),
    # Aplicar direto, sem passar por "aprovada", é permitido: às vezes ele aplica na hora.
    Status.AGUARDANDO_VOCE: frozenset({Status.APROVADA, Status.APLICADA, Status.DESCARTADA}),
    Status.APROVADA: frozenset({Status.APLICADA, Status.DESCARTADA}),
    Status.APLICADA: frozenset(
        {Status.EM_PROCESSO, Status.RECUSADA, Status.CONTRATADO, Status.SEM_RESPOSTA}
    ),
    Status.EM_PROCESSO: frozenset({Status.RECUSADA, Status.CONTRATADO, Status.SEM_RESPOSTA}),
    # Resposta que chega depois dos 90 dias reabre a vaga.
    Status.SEM_RESPOSTA: frozenset({Status.EM_PROCESSO, Status.RECUSADA, Status.CONTRATADO}),
    # Descartada pode ser reconsiderada pelo /descartadas.
    Status.DESCARTADA: frozenset({Status.AGUARDANDO_VOCE}),
    Status.CONTRATADO: frozenset(),
    Status.RECUSADA: frozenset(),
}


class InvalidTransition(ValueError):
    pass


def check_transition(current: Status, new: Status) -> None:
    if new not in TRANSITIONS[current]:
        raise InvalidTransition(f"{current} -> {new} não é permitido")


def normalize(text: str) -> str:
    """Minúsculas, sem acento e sem pontuação — para comparar a mesma vaga entre fontes."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


@dataclass(frozen=True)
class JobPosting:
    source: str  # "linkedin", "indeed", "gupy"…
    external_id: str
    title: str
    company: str
    url: str
    location: str | None = None
    description: str | None = None
    posted_at: date | None = None
    level: str | None = None  # como a fonte declara; no LinkedIn não é confiável
    is_remote: bool | None = None

    @property
    def dedup_key(self) -> str:
        return f"{normalize(self.company)}|{normalize(self.title)}"
