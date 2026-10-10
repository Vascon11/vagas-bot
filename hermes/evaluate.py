import logging
from dataclasses import dataclass, field

from hermes.db import Database
from hermes.filters import FilterRules, prefilter
from hermes.models import Status
from hermes.scoring import FatalScoringError

log = logging.getLogger(__name__)


@dataclass
class EvalResult:
    prefiltered: int = 0
    to_you: list[int] = field(default_factory=list)  # ids que viraram aguardando_voce
    low_score: int = 0
    errors: list[str] = field(default_factory=list)


def evaluate_new(
    db: Database, scorer, rules: FilterRules, score_min: int, limit: int | None = None
) -> EvalResult:
    """Leva as vagas "nova" até aguardando_voce ou descartada.

    Se a chamada ao LLM falha, a vaga continua "nova" e é tentada de novo na
    próxima rodada — nunca é descartada por erro de rede.
    """
    result = EvalResult()
    decisions = db.recent_decisions()
    for row in db.by_status(Status.NOVA)[:limit]:
        job_id = row["id"]
        if reason := prefilter(row["title"], row["level"], rules):
            db.transition(job_id, Status.DESCARTADA, f"pré-filtro: {reason}")
            result.prefiltered += 1
            continue
        try:
            s = scorer.score(row, decisions)
        except FatalScoringError as exc:
            # sem crédito ou chave inválida: as outras vagas falhariam igual
            result.errors.append(f"rodada interrompida: {exc}")
            break
        except Exception as exc:
            log.warning("nota falhou para a vaga %s: %s", job_id, exc)
            result.errors.append(f"{job_id}: {exc}")
            continue
        db.save_score(job_id, s.score, s.model_dump_json())
        db.transition(job_id, Status.AVALIADA, f"nota {s.score}")
        if s.score >= score_min:
            db.transition(job_id, Status.AGUARDANDO_VOCE)
            result.to_you.append(job_id)
        else:
            db.transition(job_id, Status.DESCARTADA, f"nota {s.score} < {score_min}")
            result.low_score += 1
    return result


def pass_unscored(db: Database, rules: FilterRules) -> EvalResult:
    """Sem LLM: o pré-filtro de título decide sozinho e o resto vai direto para você."""
    result = EvalResult()
    for row in db.by_status(Status.NOVA):
        job_id = row["id"]
        if reason := prefilter(row["title"], row["level"], rules):
            db.transition(job_id, Status.DESCARTADA, f"pré-filtro: {reason}")
            result.prefiltered += 1
            continue
        db.transition(job_id, Status.AVALIADA, "sem nota (LLM desligado)")
        db.transition(job_id, Status.AGUARDANDO_VOCE)
        result.to_you.append(job_id)
    return result
