"""O que roda sozinho: o ciclo de busca + nota e a varredura dos 90 dias.

Tudo aqui é síncrono e sem Telegram — o bot chama em uma thread e só cuida de
avisar. Assim dá para testar e rodar pela CLI do mesmo jeito.
"""

import logging
from dataclasses import dataclass

from hermes.config import Settings
from hermes.db import Database
from hermes.evaluate import EvalResult, evaluate_new, pass_unscored
from hermes.models import Status
from hermes.search import RunResult, load_queries, load_rules, run_searches

log = logging.getLogger(__name__)

NO_REPLY_DAYS = 90


@dataclass
class CycleResult:
    search: RunResult
    evaluation: EvalResult | None  # None quando não há LLM configurado


def make_sources():
    from hermes.sources.gupy import GupySource
    from hermes.sources.jobspy_source import JobSpySource

    return [JobSpySource(), GupySource()]


def make_scorer_from(settings: Settings):
    if not settings.llm_enabled:
        return None
    if not (settings.llm_api_key and settings.profile_file.exists()):
        return None
    from hermes.scoring import make_scorer

    return make_scorer(
        settings.llm_provider,
        settings.llm_api_key,
        settings.model,
        settings.profile_file.read_text(encoding="utf-8"),
        settings.llm_fallback_model or None,
    )


def run_cycle(settings: Settings, sources=None, scorer=None) -> CycleResult:
    """Busca vagas novas e dá nota. Abre a própria conexão: roda fora da thread do bot."""
    db = Database(settings.db_path)
    search = run_searches(
        load_queries(settings.searches_file),
        sources if sources is not None else make_sources(),
        db,
        pause=(settings.pause_min, settings.pause_max),
    )
    scorer = scorer if scorer is not None else make_scorer_from(settings)
    evaluation = None
    if not settings.llm_enabled:
        evaluation = pass_unscored(db, load_rules(settings.searches_file))
    elif scorer is None:
        log.warning("sem LLM ou profile.md: vagas ficam como 'nova' até configurar")
    else:
        evaluation = evaluate_new(
            db, scorer, load_rules(settings.searches_file), settings.score_min
        )
    return CycleResult(search, evaluation)


def sweep_no_reply(db: Database, days: int = NO_REPLY_DAYS) -> list[int]:
    """Candidaturas paradas há `days` dias viram sem_resposta. Devolve os ids."""
    moved = []
    for row in db.stale(days):
        db.transition(row["id"], Status.SEM_RESPOSTA, f"{days} dias sem nenhuma resposta")
        moved.append(row["id"])
    return moved
