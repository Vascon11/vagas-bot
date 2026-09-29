from datetime import date

import pytest

from hermes.db import Database
from hermes.models import InvalidTransition, JobPosting, Status


def job(**kw) -> JobPosting:
    base = dict(
        source="linkedin",
        external_id="li-1",
        title="Estágio em Desenvolvimento",
        company="Acme",
        url="https://www.linkedin.com/jobs/view/1",
        posted_at=date(2026, 9, 28),
    )
    return JobPosting(**{**base, **kw})


@pytest.fixture
def db():
    return Database(":memory:")


def test_add_job_starts_as_nova_with_event(db):
    job_id = db.add_job(job())
    assert db.get(job_id)["status"] == Status.NOVA
    assert db.get(job_id)["posted_at"] == "2026-09-28"
    assert [e["detail"] for e in db.events(job_id)] == ["nova"]


def test_same_external_id_is_duplicate(db):
    assert db.add_job(job()) is not None
    assert db.add_job(job(title="Outro título")) is None


def test_same_job_on_another_source_is_duplicate(db):
    db.add_job(job())
    # mesma vaga na Gupy, com acento e caixa diferentes
    assert db.add_job(job(source="gupy", external_id="9", title="ESTÁGIO em desenvolvimento!")) is None


def test_different_company_is_not_duplicate(db):
    db.add_job(job())
    assert db.add_job(job(external_id="li-2", company="Globex")) is not None


def test_happy_path_transitions_are_recorded(db):
    job_id = db.add_job(job())
    for s in (Status.AVALIADA, Status.AGUARDANDO_VOCE, Status.APROVADA, Status.APLICADA,
              Status.EM_PROCESSO, Status.CONTRATADO):
        db.transition(job_id, s)
    assert db.get(job_id)["status"] == Status.CONTRATADO
    assert len(db.events(job_id)) == 7


@pytest.mark.parametrize(
    "path, invalid",
    [
        ([], Status.APLICADA),  # nova não pula direto para aplicada
        ([Status.AVALIADA, Status.AGUARDANDO_VOCE, Status.APLICADA, Status.RECUSADA], Status.APLICADA),
        ([Status.DESCARTADA], Status.APLICADA),
    ],
)
def test_invalid_transitions_are_refused(db, path, invalid):
    job_id = db.add_job(job())
    for s in path:
        db.transition(job_id, s)
    before = db.get(job_id)["status"]
    with pytest.raises(InvalidTransition):
        db.transition(job_id, invalid)
    assert db.get(job_id)["status"] == before


def test_late_reply_reopens_sem_resposta(db):
    job_id = db.add_job(job())
    for s in (Status.AVALIADA, Status.AGUARDANDO_VOCE, Status.APLICADA, Status.SEM_RESPOSTA,
              Status.EM_PROCESSO):
        db.transition(job_id, s)
    assert db.get(job_id)["status"] == Status.EM_PROCESSO
