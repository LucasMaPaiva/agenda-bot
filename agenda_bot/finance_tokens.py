"""Tokens do financeiro obtidos pelo vínculo por link (/conectar_financeiro).

Ficam numa tabela própria do mesmo SQLite do bot, separada do repositório da
agenda, para que o vínculo não dependa do esquema das outras funcionalidades.
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone


class FinanceTokenStore:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS finance_tokens ("
            " role TEXT PRIMARY KEY,"
            " token TEXT NOT NULL,"
            " linked_at TEXT NOT NULL"
            ")"
        )
        return connection

    def get(self, role: str) -> str:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT token FROM finance_tokens WHERE role = ?", (role,)
            ).fetchone()
        return row[0] if row else ""

    def save(self, role: str, token: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO finance_tokens (role, token, linked_at) VALUES (?, ?, ?)"
                " ON CONFLICT(role) DO UPDATE SET token = excluded.token,"
                " linked_at = excluded.linked_at",
                (role, token, datetime.now(timezone.utc).isoformat()),
            )
