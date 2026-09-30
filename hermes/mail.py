"""Acompanhamento das candidaturas pelo e-mail.

O Outlook do Gabriel redireciona os e-mails de recrutamento para um Gmail dedicado
(a Microsoft não deixa conta pessoal registrar app sem diretório do Azure). Aqui
o Gmail é lido por IMAP com senha de app, em modo só leitura: nada é marcado
como lido nem apagado.

Cada e-mail novo é classificado pelo LLM (confirmação, teste, entrevista,
recusa…) e ligado a uma candidatura. A vaga muda de estado e o Telegram avisa.
"""

import email
import html
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Literal

from pydantic import BaseModel

from hermes.db import Database
from hermes.models import InvalidTransition, Status
from hermes.scoring import FatalScoringError

log = logging.getLogger(__name__)

MAX_BODY = 4000

MailKind = Literal[
    "confirmacao",  # recebemos sua candidatura
    "questionario",  # formulário, perguntas, vídeo
    "teste",  # teste técnico, desafio, case
    "entrevista",  # convite ou agendamento
    "recusa",
    "proposta",
    "atualizacao",  # mudou de etapa, sem pedir nada
    "nao_relacionado",  # alerta de vagas, newsletter, propaganda
]

# O que cada tipo faz com a vaga: caminho de estados a partir de cada estado atual.
# Transições que não se aplicam (ex.: recusa em vaga já recusada) são ignoradas.
_TO_PROCESS = {
    Status.APROVADA: [Status.APLICADA, Status.EM_PROCESSO],
    Status.APLICADA: [Status.EM_PROCESSO],
    Status.SEM_RESPOSTA: [Status.EM_PROCESSO],
}
PATHS: dict[str, dict[Status, list[Status]]] = {
    "confirmacao": {Status.APROVADA: [Status.APLICADA]},
    "questionario": _TO_PROCESS,
    "teste": _TO_PROCESS,
    "entrevista": _TO_PROCESS,
    "proposta": _TO_PROCESS,  # contratado é ele quem marca
    "recusa": {
        Status.APROVADA: [Status.APLICADA, Status.RECUSADA],
        Status.APLICADA: [Status.RECUSADA],
        Status.EM_PROCESSO: [Status.RECUSADA],
        Status.SEM_RESPOSTA: [Status.RECUSADA],
    },
    "atualizacao": {Status.APROVADA: [Status.APLICADA]},
}

ICONS = {
    "confirmacao": "📨", "questionario": "📝", "teste": "🧪", "entrevista": "🗓️",
    "recusa": "🚫", "proposta": "🎉", "atualizacao": "🔄",
}


class MailInfo(BaseModel):
    tipo: MailKind
    empresa: str | None
    cargo: str | None
    vaga_id: int | None  # id de uma das candidaturas listadas, ou null
    resumo: str
    prazo: str | None  # data/hora que o e-mail pede, em texto


INSTRUCTIONS = """\
Você lê e-mails que chegam para um candidato a vagas de emprego e diz do que se trata.

Tipos:
- confirmacao: a empresa ou a plataforma confirma que recebeu a candidatura.
- questionario: pede para responder perguntas, formulário, vídeo ou cadastro extra.
- teste: teste técnico, desafio, case ou prova.
- entrevista: convite, agendamento ou confirmação de entrevista.
- recusa: a empresa encerrou a participação dele no processo.
- proposta: oferta de contratação.
- atualizacao: mudança de etapa ou aviso sem pedido de ação.
- nao_relacionado: alerta de vagas, recomendação, newsletter, propaganda, qualquer
  coisa que não seja sobre uma candidatura dele.

Candidaturas ativas dele (id | empresa | cargo):
{candidates}

Regras:
- vaga_id: o id da candidatura acima de que o e-mail trata, comparando empresa e cargo.
  Se nenhuma bater com segurança, null. Nunca invente um id fora da lista.
- resumo: uma frase em português dizendo o que aconteceu e o que ele precisa fazer.
- prazo: só se o e-mail pedir algo até uma data/hora; senão null.
"""


@dataclass
class RawMail:
    uid: int
    message_id: str
    received_at: str | None
    sender: str
    subject: str
    body: str


@dataclass
class Notice:
    """Algo para avisar no Telegram."""

    mail_id: int
    kind: str
    summary: str
    deadline: str | None
    job_id: int | None  # None = relevante, mas sem vaga ligada: perguntar


# --- leitura e parse ---------------------------------------------------------


def _decode(value: str | None) -> str:
    return str(make_header(decode_header(value))) if value else ""


def _html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", raw)
    return html.unescape(re.sub(r"<[^>]+>", " ", raw))


def body_text(msg: email.message.Message) -> str:
    plain, rich = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() != "text" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        (plain if part.get_content_subtype() == "plain" else rich).append(text)
    text = "\n".join(plain) or _html_to_text("\n".join(rich))
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()[:MAX_BODY]


def parse_mail(uid: int, raw: bytes) -> RawMail:
    msg = email.message_from_bytes(raw)
    try:
        received = parsedate_to_datetime(msg["Date"]).astimezone(UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        received = None
    name, addr = parseaddr(_decode(msg["From"]))
    return RawMail(
        uid=uid,
        # Sem Message-ID (raro), o UID serve de identificador estável dentro da caixa.
        message_id=(msg["Message-ID"] or f"<uid-{uid}@local>").strip(),
        received_at=received,
        sender=f"{name} <{addr}>" if name else addr,
        subject=_decode(msg["Subject"]),
        body=body_text(msg),
    )


class GmailInbox:
    """IMAP do Gmail, só leitura. Guarda no kv até qual UID já leu.

    É o Gmail pessoal do Gabriel, então a busca é restrita aos e-mails endereçados
    ao Outlook dele (`only_to`) — os que a regra do Outlook redireciona. O resto da
    caixa nunca é baixado nem enviado ao LLM.
    """

    HOST = "imap.gmail.com"

    def __init__(self, address: str, app_password: str, only_to: str, first_run_days: int = 7):
        if not only_to:
            raise ValueError("only_to é obrigatório: sem ele o bot leria a caixa inteira")
        self._address = address
        self._password = app_password
        self._only_to = only_to
        self._first_run_days = first_run_days

    @property
    def _quoted_to(self) -> str:
        return '"' + self._only_to.replace('"', "") + '"'

    def fetch_new(self, db: Database) -> list[RawMail]:
        imap = imaplib.IMAP4_SSL(self.HOST)
        try:
            imap.login(self._address, self._password)
            imap.select("INBOX", readonly=True)
            validity = imap.response("UIDVALIDITY")[1][0].decode()
            last = db.kv_get("imap_last_uid")
            if last is None or db.kv_get("imap_uidvalidity") != validity:
                # Primeira leitura (ou a caixa foi recriada): só os últimos dias.
                since = (datetime.now(UTC) - timedelta(days=self._first_run_days)).strftime("%d-%b-%Y")
                _, data = imap.uid("SEARCH", None, "SINCE", since, "TO", self._quoted_to)
                db.kv_set("imap_uidvalidity", validity)
            else:
                _, data = imap.uid("SEARCH", None, "UID", f"{int(last) + 1}:*", "TO", self._quoted_to)
            uids = sorted(int(u) for u in data[0].split())
            if last is not None and db.kv_get("imap_uidvalidity") == validity:
                # "N:*" devolve a última mensagem mesmo quando não há nada acima de N.
                uids = [u for u in uids if u > int(last)]
            mails = []
            for uid in uids:
                _, parts = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if raw:
                    mails.append(parse_mail(uid, raw))
            return mails
        finally:
            try:
                imap.logout()
            except Exception:
                pass


# --- classificação e efeito na vaga ----------------------------------------


def format_candidates(rows) -> str:
    if not rows:
        return "(nenhuma)"
    return "\n".join(f"{r['id']} | {r['company']} | {r['title']}" for r in rows)


def classify(llm, mail: RawMail, candidates) -> MailInfo:
    text = f"De: {mail.sender}\nAssunto: {mail.subject}\n\n{mail.body}"
    info = llm.ask(INSTRUCTIONS.format(candidates=format_candidates(candidates)), text, MailInfo)
    if info.vaga_id is not None and info.vaga_id not in {r["id"] for r in candidates}:
        log.warning("LLM devolveu vaga_id %s fora da lista; ignorando", info.vaga_id)
        info.vaga_id = None
    return info


def apply_to_job(db: Database, job_id: int, kind: str, summary: str) -> None:
    """Registra o e-mail na vaga e anda o estado conforme o tipo."""
    db.add_event(job_id, "email", f"{kind}: {summary}")
    current = Status(db.get(job_id)["status"])
    for step in PATHS.get(kind, {}).get(current, []):
        try:
            db.transition(job_id, step, f"e-mail: {kind}")
        except InvalidTransition:
            log.warning("vaga %s: %s -> %s não se aplica", job_id, current, step)
            break


def check_mail(db: Database, inbox, llm) -> list[Notice]:
    """Lê os e-mails novos, aplica nas vagas e devolve o que avisar.

    O ponteiro do IMAP só anda depois que o e-mail foi processado: se o LLM
    falhar no meio, a próxima rodada recomeça daquele e-mail.
    """
    notices = []
    for mail in inbox.fetch_new(db):
        if db.mail_seen(mail.message_id):
            db.kv_set("imap_last_uid", str(mail.uid))
            continue
        candidates = db.active_applications()
        try:
            info = classify(llm, mail, candidates)
        except FatalScoringError:
            raise
        except Exception as exc:
            log.warning("e-mail %s não classificado: %s", mail.uid, exc)
            break  # tenta de novo na próxima rodada, sem pular este
        mail_id = db.add_mail(mail.message_id, mail.received_at, mail.sender, mail.subject,
                              info.tipo, info.empresa, info.resumo, info.vaga_id)
        db.kv_set("imap_last_uid", str(mail.uid))
        if info.tipo == "nao_relacionado":
            continue
        if info.vaga_id is not None:
            apply_to_job(db, info.vaga_id, info.tipo, info.resumo)
        notices.append(Notice(mail_id, info.tipo, info.resumo, info.prazo, info.vaga_id))
    return notices
