"""Modo canal: busca, pré-filtro e posta no Discord, sem guardar vaga em disco.

Não usa banco nem Telegram. Para não repetir vaga, guarda só a chave das vagas
já postadas, na memória do processo, por `remember_days`. Num restart a memória
zera, então a janela de busca (`hours_old` no searches.yaml) deve ser curta, perto
do intervalo entre buscas: assim o máximo que se repete é o que saiu nessa janela.

Cada vaga vai para a thread do seu nível (`discord.threads` no searches.yaml). O
nível sai das regras de filters.py; o LLM só entra, se ligado
(`HERMES_LEVEL_LLM_PER_DAY`), nas vagas que as regras não resolveram.
"""

import asyncio
import logging
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from hermes.config import Settings
from hermes.discord import post_card
from hermes.filters import FilterRules, explain_level, prefilter
from hermes.models import JobPosting
from hermes.scoring import FatalScoringError, make_scorer  # os SDKs só carregam se usados
from hermes.search import load_queries, load_rules, load_threads, run_searches
from hermes.sources.gupy import GupySource
from hermes.sources.jobspy_source import JobSpySource

log = logging.getLogger(__name__)

# Webhook aceita uns 30 posts por minuto por canal; 2,5 s entre posts fica abaixo.
POST_GAP = 2.5
# Vagas por chamada ao LLM: uma chamada por ciclo dá conta do volume normal.
LLM_BATCH = 15

LEVEL_LABELS = {"estagio": "Estágio", "aprendiz": "Aprendiz", "trainee": "Trainee",
                "junior": "Júnior", "pleno": "Pleno", "senior": "Sênior"}


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


class LevelLLM:
    """O LLM como último recurso para o nível, com teto de vagas por dia.

    O teto conta vagas enviadas, não chamadas: é o que define o custo. O resultado
    fica guardado até o fim do dia, então uma vaga que falhou no Discord e volta na
    rodada seguinte não é paga de novo. Tudo em memória: um restart zera o dia.
    """

    def __init__(self, scorer, per_day: int, today):
        self._scorer = scorer
        self._per_day = per_day
        self._today = today
        self._day: date | None = None
        self._used = 0
        self._known: dict[str, str | None] = {}  # dedup_key -> nível (None = indefinido)

    def _roll(self) -> None:
        if (day := self._today()) != self._day:
            self._day, self._used, self._known = day, 0, {}

    async def levels(self, postings: list[JobPosting]) -> dict[str, str]:
        """dedup_key -> nível, só das vagas que o LLM conseguiu classificar."""
        self._roll()
        room = min(LLM_BATCH, self._per_day - self._used)  # nunca negativo
        todo = [p for p in postings if p.dedup_key not in self._known][:room]
        if todo:
            self._used += len(todo)
            try:
                found = await asyncio.to_thread(
                    self._scorer.levels, [(p.title, p.description) for p in todo]
                )
            except FatalScoringError as exc:
                # chave inválida, cota ou crédito: as próximas falhariam igual
                self._used = self._per_day
                log.warning("LLM do nível desligado até amanhã: %s", exc)
            except Exception as exc:
                # erro passageiro: não guarda, a vaga tenta de novo se ainda houver teto
                log.warning("LLM do nível falhou (%s): vagas vão para outros", type(exc).__name__)
            else:
                self._known.update((p.dedup_key, lvl) for p, lvl in zip(todo, found))
        return {p.dedup_key: lvl for p in postings if (lvl := self._known.get(p.dedup_key))}


def make_level_llm(settings: Settings) -> LevelLLM | None:
    if settings.level_llm_per_day <= 0:
        return None
    if not settings.llm_api_key:
        log.warning("HERMES_LEVEL_LLM_PER_DAY ligado, mas sem chave do %s: LLM do nível desligado",
                    settings.llm_provider)
        return None
    scorer = make_scorer(settings.llm_provider, settings.llm_api_key, settings.model, "",
                         settings.llm_fallback_model or None)
    zone = ZoneInfo(settings.timezone)
    return LevelLLM(scorer, settings.level_llm_per_day, lambda: datetime.now(zone).date())


def to_post(found: list[JobPosting], rules: FilterRules, seen: Seen) -> list[JobPosting]:
    """As vagas da rodada que passam no pré-filtro e ainda não foram postadas."""
    return [p for p in found if p.dedup_key not in seen and prefilter(p.title, p.level, rules) is None]


def as_row(p: JobPosting, level: str | None = None, why: str | None = None) -> dict:
    """O formato que o card do Discord espera, sem id nem nota, com o nível no rodapé."""
    return {"id": None, "title": p.title, "company": p.company, "location": p.location,
            "url": p.url, "source": p.source, "score": None, "score_reason": None,
            "nivel": f"{LEVEL_LABELS[level]} {why}" if level else "nível não identificado"}


def thread_for(level: str | None, threads: dict[str, str]) -> str | None:
    """A thread do nível; sem ela, a de "outros"; sem as duas, a da URL do webhook."""
    return threads.get(level) or threads.get("outros")


async def cycle(settings: Settings, seen: Seen, post=post_card, sources=None,
                llm: LevelLLM | None = None) -> int:
    """Uma rodada. Devolve quantas vagas foram postadas."""
    queries = load_queries(settings.searches_file)
    rules = load_rules(settings.searches_file)
    threads = load_threads(settings.searches_file)
    result = await asyncio.to_thread(
        run_searches, queries, sources or [JobSpySource(), GupySource()], None,
        pause=(settings.pause_min, settings.pause_max),
    )
    pending = to_post(result.found, rules, seen)
    levels = {p.dedup_key: explain_level(p.title, p.level, p.description) for p in pending}
    by_llm: dict[str, str] = {}
    if llm and (unknown := [p for p in pending if levels[p.dedup_key][0] is None]):
        by_llm = await llm.levels(unknown)
        levels.update((key, (lvl, "pela IA")) for key, lvl in by_llm.items())
    posted = 0
    for p in pending:
        level, why = levels[p.dedup_key]
        # Falha (Discord fora, 4xx) não marca como vista: tenta de novo na próxima rodada.
        if await post(settings.discord_webhook_url, as_row(p, level, why),
                      thread_id=thread_for(level, threads)):
            seen.add(p.dedup_key)
            posted += 1
        await asyncio.sleep(POST_GAP)
    seen.prune()
    log.warning("canal: %d vagas encontradas, %d postadas (%d com nível pela IA), %d na memória",
                len(result.found), posted, len(by_llm), len(seen))
    return posted


async def _heartbeat(settings: Settings) -> None:
    """Toca data/heartbeat: o healthcheck do Docker considera o bot vivo por ele."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    while True:
        (settings.data_dir / "heartbeat").touch()
        await asyncio.sleep(60)


async def run_forever(settings: Settings) -> None:
    seen = Seen(settings.remember_days)
    llm = make_level_llm(settings)
    beat = asyncio.create_task(_heartbeat(settings))
    try:
        while True:
            try:
                await cycle(settings, seen, llm=llm)
            except Exception:
                log.exception("ciclo do canal falhou")
            await asyncio.sleep(settings.search_every_hours * 3600)
    finally:
        beat.cancel()
