"""Modo canal: busca, pré-filtro e posta no Discord, sem guardar vaga em disco.

Não usa banco, Telegram nem LLM. Para não repetir vaga, guarda só a chave das vagas
já postadas, na memória do processo, por `remember_days`. Num restart a memória
zera, então a janela de busca (`hours_old` no searches.yaml) deve ser curta, perto
do intervalo entre buscas: assim o máximo que se repete é o que saiu nessa janela.
"""

import asyncio
import logging
import time

from hermes.config import Settings
from hermes.discord import post_card
from hermes.filters import FilterRules, prefilter
from hermes.models import JobPosting
from hermes.search import load_queries, load_rules, run_searches
from hermes.sources.gupy import GupySource
from hermes.sources.jobspy_source import JobSpySource

log = logging.getLogger(__name__)

# Webhook aceita uns 30 posts por minuto por canal; 2,5 s entre posts fica abaixo.
POST_GAP = 2.5


class Seen:
    """Chaves de vagas já postadas, só em memória, esquecidas depois de `days`."""

    def __init__(self, days: float, now=time.monotonic):
        self._ttl = days * 86400
        self._now = now
        self._at: dict[str, float] = {}

    def __contains__(self, key: str) -> bool:
        return key in self._at

    def add(self, key: str) -> None:
        self._at[key] = self._now()

    def prune(self) -> None:
        cutoff = self._now() - self._ttl
        self._at = {k: t for k, t in self._at.items() if t >= cutoff}

    def __len__(self) -> int:
        return len(self._at)


def to_post(found: list[JobPosting], rules: FilterRules, seen: Seen) -> list[JobPosting]:
    """As vagas da rodada que passam no pré-filtro e ainda não foram postadas."""
    return [p for p in found if p.dedup_key not in seen and prefilter(p.title, p.level, rules) is None]


def as_row(p: JobPosting) -> dict:
    """O formato que o card do Discord espera, sem id nem nota."""
    return {"id": None, "title": p.title, "company": p.company, "location": p.location,
            "url": p.url, "source": p.source, "score": None, "score_reason": None}


async def cycle(settings: Settings, seen: Seen, post=post_card, sources=None) -> int:
    """Uma rodada. Devolve quantas vagas foram postadas."""
    queries = load_queries(settings.searches_file)
    rules = load_rules(settings.searches_file)
    result = await asyncio.to_thread(
        run_searches, queries, sources or [JobSpySource(), GupySource()], None,
        pause=(settings.pause_min, settings.pause_max),
    )
    posted = 0
    for p in to_post(result.found, rules, seen):
        # Falha (Discord fora, 4xx) não marca como vista: tenta de novo na próxima rodada.
        if await post(settings.discord_webhook_url, as_row(p)):
            seen.add(p.dedup_key)
            posted += 1
        await asyncio.sleep(POST_GAP)
    seen.prune()
    log.warning("canal: %d vagas encontradas, %d postadas, %d na memória",
                len(result.found), posted, len(seen))
    return posted


async def _heartbeat(settings: Settings) -> None:
    """Toca data/heartbeat: o healthcheck do Docker considera o bot vivo por ele."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    while True:
        (settings.data_dir / "heartbeat").touch()
        await asyncio.sleep(60)


async def run_forever(settings: Settings) -> None:
    seen = Seen(settings.remember_days)
    beat = asyncio.create_task(_heartbeat(settings))
    try:
        while True:
            try:
                await cycle(settings, seen)
            except Exception:
                log.exception("ciclo do canal falhou")
            await asyncio.sleep(settings.search_every_hours * 3600)
    finally:
        beat.cancel()
