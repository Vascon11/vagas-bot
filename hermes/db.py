import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hermes.models import JobPosting, Status, check_transition

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    dedup_key     TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    url           TEXT NOT NULL,
    location      TEXT,
    description   TEXT,
    posted_at     TEXT,
    level         TEXT,
    is_remote     INTEGER,
    score         INTEGER,
    score_reason  TEXT,             -- JSON do scoring.Score
    decision      TEXT,             -- "aprovou" / "descartou", quando o Gabriel decide
    notified_at   TEXT,             -- quando o card foi enviado no Telegram
    status        TEXT NOT NULL,
    found_at      TEXT NOT NULL,
    status_at     TEXT NOT NULL,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS jobs_dedup ON jobs (dedup_key);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status);

CREATE TABLE IF NOT EXISTS mails (
    id           INTEGER PRIMARY KEY,
    message_id   TEXT NOT NULL UNIQUE,  -- Message-ID do e-mail: não processa duas vezes
    received_at  TEXT,
    sender       TEXT,
    subject      TEXT,
    kind         TEXT NOT NULL,         -- tipo classificado (mail.MailKind)
    company      TEXT,
    summary      TEXT,
    job_id       INTEGER REFERENCES jobs (id),
    created_at   TEXT NOT NULL
);

-- Estado pequeno de chave/valor (ex.: até qual UID do IMAP já foi lido).
CREATE TABLE IF NOT EXISTS kv (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY,
    job_id   INTEGER NOT NULL REFERENCES jobs (id),
    at       TEXT NOT NULL,
    kind     TEXT NOT NULL,   -- "status" ou o tipo do e-mail na fase 4
    detail   TEXT
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        # colunas que não existiam nos bancos das fases anteriores
        for col in ("decision", "notified_at"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")

    def add_job(self, job: JobPosting) -> int | None:
        """Grava a vaga como "nova". Devolve o id, ou None se ela já existe.

        Já existe = mesmo (source, external_id) ou mesma empresa+título vinda de
        outra fonte (a mesma vaga publicada no LinkedIn e na Gupy).
        """
        dup = self.conn.execute(
            "SELECT 1 FROM jobs WHERE (source = ? AND external_id = ?) OR dedup_key = ?",
            (job.source, job.external_id, job.dedup_key),
        ).fetchone()
        if dup:
            return None
        row = asdict(job)
        row["posted_at"] = job.posted_at.isoformat() if job.posted_at else None
        now = _now()
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO jobs (source, external_id, dedup_key, title, company, url,
                       location, description, posted_at, level, is_remote,
                       status, found_at, status_at)
                   VALUES (:source, :external_id, :dedup_key, :title, :company, :url,
                       :location, :description, :posted_at, :level, :is_remote,
                       :status, :now, :now)""",
                {**row, "dedup_key": job.dedup_key, "status": Status.NOVA, "now": now},
            )
            self.conn.execute(
                "INSERT INTO events (job_id, at, kind, detail) VALUES (?, ?, 'status', ?)",
                (cur.lastrowid, now, Status.NOVA),
            )
        return cur.lastrowid

    def transition(self, job_id: int, new: Status, detail: str | None = None) -> None:
        current = Status(self.get(job_id)["status"])
        check_transition(current, new)
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET status = ?, status_at = ? WHERE id = ?", (new, now, job_id)
            )
            self.conn.execute(
                "INSERT INTO events (job_id, at, kind, detail) VALUES (?, ?, 'status', ?)",
                (job_id, now, f"{current} -> {new}" + (f": {detail}" if detail else "")),
            )

    def get(self, job_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return row

    def by_status(self, status: Status) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY found_at", (status,)
        ).fetchall()

    def events(self, job_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()

    def save_score(self, job_id: int, score: int, reason_json: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET score = ?, score_reason = ? WHERE id = ?",
                (score, reason_json, job_id),
            )

    def set_decision(self, job_id: int, decision: str) -> None:
        assert decision in ("aprovou", "descartou")
        with self.conn:
            self.conn.execute("UPDATE jobs SET decision = ? WHERE id = ?", (decision, job_id))

    def recent_decisions(self, limit: int = 10) -> list[sqlite3.Row]:
        """As últimas vagas que o Gabriel aprovou ou descartou — exemplos para a nota."""
        return self.conn.execute(
            """SELECT title, company, location, decision FROM jobs
               WHERE decision IS NOT NULL ORDER BY status_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    def to_notify(self) -> list[sqlite3.Row]:
        """Vagas esperando o Gabriel cujo card ainda não foi enviado, melhores primeiro."""
        return self.conn.execute(
            """SELECT * FROM jobs WHERE status = ? AND notified_at IS NULL
               ORDER BY score DESC""",
            (Status.AGUARDANDO_VOCE,),
        ).fetchall()

    def mark_notified(self, job_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE jobs SET notified_at = ? WHERE id = ?", (_now(), job_id))

    def stale(self, days: int, statuses=(Status.APLICADA, Status.EM_PROCESSO)) -> list[sqlite3.Row]:
        """Candidaturas sem nenhuma mudança de estado há mais de `days` dias."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        marks = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks}) AND status_at < ?",
            (*statuses, cutoff),
        ).fetchall()

    def counts(self) -> dict[str, int]:
        return dict(self.conn.execute("SELECT status, count(*) FROM jobs GROUP BY status"))

    # --- e-mail -------------------------------------------------------------

    def kv_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def mail_seen(self, message_id: str) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM mails WHERE message_id = ?", (message_id,)
        ).fetchone())

    def add_mail(self, message_id, received_at, sender, subject, kind, company, summary,
                 job_id=None) -> int:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO mails (message_id, received_at, sender, subject, kind, company,
                       summary, job_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_id, received_at, sender, subject, kind, company, summary, job_id, _now()),
            )
        return cur.lastrowid

    def get_mail(self, mail_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM mails WHERE id = ?", (mail_id,)).fetchone()
        if row is None:
            raise KeyError(mail_id)
        return row

    def link_mail(self, mail_id: int, job_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE mails SET job_id = ? WHERE id = ?", (job_id, mail_id))

    def add_event(self, job_id: int, kind: str, detail: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO events (job_id, at, kind, detail) VALUES (?, ?, ?, ?)",
                (job_id, _now(), kind, detail),
            )

    def active_applications(self) -> list[sqlite3.Row]:
        """Vagas que podem receber resposta por e-mail: as que ele aprovou em diante."""
        return self.conn.execute(
            """SELECT * FROM jobs WHERE status IN (?, ?, ?, ?) ORDER BY status_at DESC""",
            (Status.APROVADA, Status.APLICADA, Status.EM_PROCESSO, Status.SEM_RESPOSTA),
        ).fetchall()
