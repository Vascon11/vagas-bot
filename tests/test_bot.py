import json
from datetime import UTC, datetime, timedelta

import pytest

from hermes.bot import apply_action, card_keyboard, card_text, summary_text
from hermes.config import Settings
from hermes.db import Database
from hermes.jobs import run_cycle, sweep_no_reply
from hermes.models import JobPosting, Status
from hermes.scoring import Score


def add(db, title="Estágio Dev <Python>", status_path=(Status.AVALIADA, Status.AGUARDANDO_VOCE), n=[0]):
    n[0] += 1
    job_id = db.add_job(JobPosting(source="gupy", external_id=str(n[0]), title=title,
                                   company=f"Acme & Cia {n[0]}", url="https://acme.gupy.io/job/1"))
    s = Score(score=80, recomendacao="aplicar", nivel_detectado="estagio",
              modelo_trabalho="hibrido", motivos=["Python <3"], alertas=["prazo curto"])
    db.save_score(job_id, 80, s.model_dump_json())
    for st in status_path:
        db.transition(job_id, st)
    return job_id


@pytest.fixture
def db():
    return Database(":memory:")


def callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def test_card_escapes_html_and_shows_score(db):
    text = card_text(db.get(add(db)))
    assert "&lt;Python&gt;" in text and "Acme &amp; Cia" in text and "Python &lt;3" in text
    assert "<b>Nota 80</b>" in text and "⚠️ prazo curto" in text


def test_buttons_follow_the_status(db):
    job_id = add(db)
    assert callbacks(card_keyboard(db.get(job_id))) == [f"ok:{job_id}", f"no:{job_id}", f"ap:{job_id}"]
    apply_action(db, "ok", job_id)
    assert callbacks(card_keyboard(db.get(job_id))) == [f"ap:{job_id}", f"no:{job_id}"]
    apply_action(db, "ap", job_id)
    assert callbacks(card_keyboard(db.get(job_id))) == []  # só o link


def test_actions_record_decision_for_scoring(db):
    a, b = add(db), add(db)
    assert apply_action(db, "ok", a) is None
    assert apply_action(db, "no", b) is None
    assert db.get(a)["decision"] == "aprovou" and db.get(b)["decision"] == "descartou"
    assert {d["decision"] for d in db.recent_decisions()} == {"aprovou", "descartou"}


def test_double_click_does_not_break(db):
    job_id = add(db)
    assert apply_action(db, "no", job_id) is None
    problem = apply_action(db, "ap", job_id)  # card antigo, vaga já descartada
    assert "descartada" in problem and db.get(job_id)["status"] == Status.DESCARTADA


def test_to_notify_only_unsent_waiting_jobs(db):
    a = add(db)
    add(db, status_path=(Status.DESCARTADA,))
    assert [r["id"] for r in db.to_notify()] == [a]
    db.mark_notified(a)
    assert db.to_notify() == []


def test_sweep_moves_only_old_applications(db):
    old = add(db, status_path=(Status.AVALIADA, Status.AGUARDANDO_VOCE, Status.APLICADA))
    recent = add(db, status_path=(Status.AVALIADA, Status.AGUARDANDO_VOCE, Status.APLICADA))
    long_ago = (datetime.now(UTC) - timedelta(days=91)).isoformat(timespec="seconds")
    db.conn.execute("UPDATE jobs SET status_at = ? WHERE id = ?", (long_ago, old))
    assert sweep_no_reply(db) == [old]
    assert db.get(old)["status"] == Status.SEM_RESPOSTA
    assert db.get(recent)["status"] == Status.APLICADA


def test_summary_lists_only_nonzero(db):
    add(db)
    assert "esperando você: <b>1</b>" in summary_text(db)
    assert "recusada" not in summary_text(db)


def test_run_cycle_end_to_end_without_network(tmp_path):
    (tmp_path / "s.yaml").write_text(
        "searches:\n  - name: a\n    term: estagio\nfiltros:\n  excluir_no_titulo: [pleno]\n"
    )

    class Source:
        def search(self, q):
            return [JobPosting(source="gupy", external_id="1", title="Estágio Dev", company="A", url="u"),
                    JobPosting(source="gupy", external_id="2", title="Dev Pleno", company="B", url="u")]

    class Scorer:
        def score(self, row, decisions):
            return Score(score=75, recomendacao="aplicar", nivel_detectado="estagio",
                         modelo_trabalho="remoto", motivos=[], alertas=[])

    settings = Settings(data_dir=tmp_path, searches_file=tmp_path / "s.yaml", pause_min=0, pause_max=0)
    r = run_cycle(settings, sources=[Source()], scorer=Scorer())
    assert len(r.search.new_ids) == 2
    assert len(r.evaluation.to_you) == 1 and r.evaluation.prefiltered == 1
    assert [row["title"] for row in Database(settings.db_path).to_notify()] == ["Estágio Dev"]


def test_run_cycle_without_llm_sends_everything_that_passes_the_title_filter(tmp_path):
    (tmp_path / "s.yaml").write_text(
        "searches:\n  - name: a\n    term: estagio\nfiltros:\n  excluir_no_titulo: [pleno]\n"
    )

    class Source:
        def search(self, q):
            return [JobPosting(source="gupy", external_id="1", title="Estágio Dev", company="A", url="u"),
                    JobPosting(source="gupy", external_id="2", title="Dev Pleno", company="B", url="u")]

    class Scorer:
        def score(self, row, decisions):
            raise AssertionError("com o LLM desligado ninguém dá nota")

    settings = Settings(data_dir=tmp_path, searches_file=tmp_path / "s.yaml", pause_min=0, pause_max=0,
                        llm_enabled=False)
    r = run_cycle(settings, sources=[Source()], scorer=Scorer())
    assert len(r.evaluation.to_you) == 1 and r.evaluation.prefiltered == 1
    [row] = Database(settings.db_path).to_notify()
    assert row["title"] == "Estágio Dev" and row["score"] is None
    assert "Nota" not in card_text(row)
