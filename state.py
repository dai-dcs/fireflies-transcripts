"""Tiny SQLite-backed idempotency ledger.

Every transcript we successfully upload gets a row here, keyed by Fireflies
meeting/transcript ID. Both the webhook listener and the backfill/reconcile
scripts consult this before uploading, so re-delivered webhooks or overlapping
runs never produce duplicate or wasted uploads.
"""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

_LOCK = threading.Lock()


class StateStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS uploaded_transcripts (
                    transcript_id TEXT PRIMARY KEY,
                    object_name   TEXT NOT NULL,
                    uploaded_at   TEXT NOT NULL,
                    source        TEXT NOT NULL
                )
                """
            )
            # Generic key/value checkpoint store. Used by backfill.py to remember
            # how far into pagination it got, so an interrupted run resumes near
            # where it left off instead of re-scanning from the very start.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def already_uploaded(self, transcript_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM uploaded_transcripts WHERE transcript_id = ?",
                (transcript_id,),
            ).fetchone()
            return row is not None

    def mark_uploaded(self, transcript_id: str, object_name: str, source: str):
        with _LOCK:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO uploaded_transcripts
                        (transcript_id, object_name, uploaded_at, source)
                    VALUES (?, ?, ?, ?)
                    """,
                    (transcript_id, object_name, datetime.now(timezone.utc).isoformat(), source),
                )
                conn.commit()

    def get_checkpoint(self, key: str, default=None):
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM checkpoints WHERE key = ?", (key,)).fetchone()
            return row[0] if row else default

    def set_checkpoint(self, key: str, value: str):
        with _LOCK:
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO checkpoints (key, value) VALUES (?, ?)",
                    (key, value),
                )
                conn.commit()

    def clear_checkpoint(self, key: str):
        with _LOCK:
            with self._conn() as conn:
                conn.execute("DELETE FROM checkpoints WHERE key = ?", (key,))
                conn.commit()
