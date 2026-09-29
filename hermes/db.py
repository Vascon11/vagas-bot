import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
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
    score_reason  TEXT,
    status        TEXT NOT NULL,
    found_at      TEXT NOT NULL,
    status_at     TEXT NOT NULL,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS jobs_dedup ON jobs (dedup_key);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status);

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
