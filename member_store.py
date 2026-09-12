"""Persistent data model for optional Small Days member features.

Authentication is deliberately kept outside this module. Callers must pass a
verified provider subject; never accept a browser-supplied user identifier as
proof of identity.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path


class MemberStore:
    def __init__(self, database_path):
        self.database_path = Path(database_path)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialise(self):
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS members (
                    id INTEGER PRIMARY KEY,
                    provider_subject TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS saved_events (
                    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
                    event_key TEXT NOT NULL,
                    saved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (member_id, event_key)
                );

                CREATE TABLE IF NOT EXISTS event_interest (
                    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
                    event_key TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (member_id, event_key)
                );
                """
            )

    def upsert_member(self, provider_subject, email):
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO members (provider_subject, email)
                VALUES (?, ?)
                ON CONFLICT(provider_subject) DO UPDATE SET
                    email = excluded.email,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (provider_subject, email),
            )
            return connection.execute(
                "SELECT id, provider_subject, email FROM members WHERE provider_subject = ?",
                (provider_subject,),
            ).fetchone()

    def save_event(self, provider_subject, event_key):
        with self._connection() as connection:
            member = connection.execute(
                "SELECT id FROM members WHERE provider_subject = ?", (provider_subject,)
            ).fetchone()
            if member is None:
                raise LookupError("Member does not exist")
            connection.execute(
                "INSERT OR IGNORE INTO saved_events (member_id, event_key) VALUES (?, ?)",
                (member["id"], event_key),
            )

    def saved_event_keys(self, provider_subject):
        with self._connection() as connection:
            return [
                row["event_key"]
                for row in connection.execute(
                    """
                    SELECT saved_events.event_key
                    FROM saved_events
                    JOIN members ON members.id = saved_events.member_id
                    WHERE members.provider_subject = ?
                    ORDER BY saved_events.saved_at DESC
                    """,
                    (provider_subject,),
                )
            ]

    def remove_saved_event(self, provider_subject, event_key):
        """Remove one saved event for an already verified member."""
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM saved_events
                WHERE event_key = ?
                  AND member_id = (
                      SELECT id FROM members WHERE provider_subject = ?
                  )
                """,
                (event_key, provider_subject),
            )
