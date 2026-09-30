from email.message import EmailMessage

import pytest

from hermes.db import Database
from hermes.mail import GmailInbox, MailInfo, RawMail, apply_to_job, check_mail, classify, parse_mail
from hermes.models import JobPosting, Status


def job(db, company, status_path, n=[0]):
    n[0] += 1
    job_id = db.add_job(JobPosting(source="gupy", external_id=str(n[0]), title=f"Estágio {n[0]}",
                                   company=company, url="u"))
    for s in status_path:
        db.transition(job_id, s)
    return job_id


APPLIED = (Status.AVALIADA, Status.AGUARDANDO_VOCE, Status.APLICADA)
APPROVED = (Status.AVALIADA, Status.AGUARDANDO_VOCE, Status.APROVADA)


def info(**kw) -> MailInfo:
    base = dict(tipo="entrevista", empresa="FGC", cargo=None, vaga_id=None,
                resumo="Convite para entrevista", prazo=None)
    return MailInfo(**{**base, **kw})


class FakeLLM:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def ask(self, instructions, text, schema):
        self.prompts.append((instructions, text))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


class FakeInbox:
    def __init__(self, mails):
        self.mails = mails

    def fetch_new(self, db):
        return self.mails


def raw(uid, subject="Entrevista", mid=None):
    return RawMail(uid=uid, message_id=mid or f"<{uid}@x>", received_at=None,
                   sender="Gupy <no-reply@gupy.io>", subject=subject, body="corpo")


# --- parse ---------------------------------------------------------------------


def test_parse_decodes_subject_and_prefers_plain_text():
    m = EmailMessage()
    m["From"] = "=?utf-8?q?Recrutamento_S=C3=A3o_Paulo?= <rh@acme.com>"
    m["Subject"] = "=?utf-8?q?Convite_para_entrevista_=E2=80=94_Est=C3=A1gio?="
    m["Message-ID"] = "<abc@acme>"
    m["Date"] = "Tue, 29 Sep 2026 10:00:00 -0300"
    m.set_content("Olá Gabriel,\n\n\n\nQuinta às 14h?")
    m.add_alternative("<p>versão <b>html</b></p>", subtype="html")
    r = parse_mail(7, m.as_bytes())
    assert r.subject == "Convite para entrevista — Estágio"
    assert r.sender == "Recrutamento São Paulo <rh@acme.com>"
    assert r.received_at == "2026-09-29T13:00:00+00:00"
    assert "Quinta às 14h?" in r.body and "html" not in r.body


def test_parse_html_only_is_converted_to_text():
    m = EmailMessage()
    m["Subject"] = "x"
    m.set_content("<style>.a{}</style><p>Teste &amp; desafio</p><br>fim", subtype="html")
    body = parse_mail(1, m.as_bytes()).body
    assert "Teste & desafio" in body and ".a{}" not in body and "<p>" not in body


def test_inbox_refuses_to_read_whole_mailbox():
    with pytest.raises(ValueError):
        GmailInbox("a@gmail.com", "senha", only_to="")


# --- efeito na vaga -------------------------------------------------------------


@pytest.mark.parametrize(
    "path, kind, final",
    [
        (APPROVED, "confirmacao", Status.APLICADA),  # aplicou e esqueceu de clicar
        (APPLIED, "entrevista", Status.EM_PROCESSO),
        (APPROVED, "teste", Status.EM_PROCESSO),  # aprovada -> aplicada -> em processo
        (APPLIED, "recusa", Status.RECUSADA),
        (APPROVED, "recusa", Status.RECUSADA),
        (APPLIED + (Status.SEM_RESPOSTA,), "entrevista", Status.EM_PROCESSO),  # resposta tardia
        (APPLIED, "proposta", Status.EM_PROCESSO),  # contratado é ele quem marca
        (APPLIED, "atualizacao", Status.APLICADA),
    ],
)
def test_apply_to_job_moves_status(path, kind, final):
    db = Database(":memory:")
    j = job(db, "FGC", path)
    apply_to_job(db, j, kind, "resumo")
    assert db.get(j)["status"] == final
    assert any(e["kind"] == "email" for e in db.events(j))


def test_recusa_on_refused_job_only_records_event():
    db = Database(":memory:")
    j = job(db, "FGC", APPLIED + (Status.RECUSADA,))
    apply_to_job(db, j, "recusa", "de novo")
    assert db.get(j)["status"] == Status.RECUSADA


# --- classificação --------------------------------------------------------------


def test_classify_drops_hallucinated_job_id_and_lists_candidates():
    db = Database(":memory:")
    j = job(db, "FGC", APPLIED)
    llm = FakeLLM([info(vaga_id=999)])
    result = classify(llm, raw(1), db.active_applications())
    assert result.vaga_id is None
    assert f"{j} | FGC | Estágio" in llm.prompts[0][0]


def test_check_mail_routes_and_notifies():
    db = Database(":memory:")
    fgc = job(db, "FGC", APPLIED)
    llm = FakeLLM([
        info(vaga_id=fgc, prazo="quinta 14h"),
        info(tipo="nao_relacionado"),
        info(tipo="teste", empresa="Desconhecida"),  # relevante, mas sem vaga
    ])
    notices = check_mail(db, FakeInbox([raw(1), raw(2), raw(3)]), llm)

    assert db.get(fgc)["status"] == Status.EM_PROCESSO
    assert [(n.kind, n.job_id) for n in notices] == [("entrevista", fgc), ("teste", None)]
    assert notices[0].deadline == "quinta 14h"
    assert db.kv_get("imap_last_uid") == "3"


def test_same_message_is_not_processed_twice():
    db = Database(":memory:")
    llm = FakeLLM([info(tipo="atualizacao")])
    check_mail(db, FakeInbox([raw(1, mid="<same@x>")]), llm)
    assert check_mail(db, FakeInbox([raw(2, mid="<same@x>")]), llm) == []
    assert len(llm.prompts) == 1


def test_llm_error_does_not_skip_the_mail():
    db = Database(":memory:")
    llm = FakeLLM([info(tipo="atualizacao"), RuntimeError("503"), info(tipo="atualizacao")])
    check_mail(db, FakeInbox([raw(1), raw(2), raw(3)]), llm)
    # parou no 2: o ponteiro fica no 1, e o 2 é tentado de novo na próxima rodada
    assert db.kv_get("imap_last_uid") == "1"
    assert not db.mail_seen("<2@x>") and not db.mail_seen("<3@x>")
