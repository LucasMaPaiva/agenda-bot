"""Persistência SQLite do bot.

O repositório não conhece Telegram, Google ou modelos de linguagem. Isso mantém as
regras de persistência isoladas e permite trocar o restante da aplicação sem alterar
o formato do banco.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional
from zoneinfo import ZoneInfo


class AgendaRepository:
    def __init__(
        self, database_path: str, timezone: ZoneInfo, reminder_minutes: int
    ) -> None:
        self.database_path = database_path
        self.timezone = timezone
        self.reminder_minutes = reminder_minutes

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS usuarios (
                    role TEXT PRIMARY KEY CHECK (role IN ('Matheus', 'Letícia')),
                    telegram_user_id INTEGER NOT NULL UNIQUE,
                    chat_id INTEGER NOT NULL,
                    registered_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notificacoes (
                    event_id TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    reminder_minutes INTEGER NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (event_id, start_time, chat_id, reminder_minutes)
                );

                CREATE TABLE IF NOT EXISTS lembretes_avulsos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL CHECK (role IN ('Matheus', 'Letícia')),
                    chat_id INTEGER NOT NULL,
                    texto TEXT NOT NULL,
                    reminder_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pendente'
                        CHECK (status IN ('pendente', 'enviando'))
                );

                CREATE INDEX IF NOT EXISTS idx_lembretes_avulsos_due
                    ON lembretes_avulsos (status, reminder_at);

                CREATE TABLE IF NOT EXISTS preferencias (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    texto TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS configuracoes (
                    role TEXT NOT NULL,
                    chave TEXT NOT NULL,
                    valor TEXT NOT NULL,
                    PRIMARY KEY (role, chave)
                );

                CREATE TABLE IF NOT EXISTS rotinas (
                    role TEXT NOT NULL,
                    nome TEXT NOT NULL,
                    eventos_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (role, nome)
                );

                CREATE TABLE IF NOT EXISTS acoes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    tipo TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    undone_at TEXT
                );

                CREATE TABLE IF NOT EXISTS resumos_enviados (
                    role TEXT NOT NULL,
                    data TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (role, data)
                );

                CREATE TABLE IF NOT EXISTS resumos_proativos (
                    role TEXT NOT NULL,
                    data TEXT NOT NULL,
                    tipo TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (role, data, tipo)
                );

                CREATE TABLE IF NOT EXISTS conversas (
                    role TEXT PRIMARY KEY,
                    historico_json TEXT NOT NULL,
                    historico_updated_at TEXT NOT NULL,
                    estado_json TEXT NOT NULL,
                    estado_updated_at TEXT NOT NULL
                );
                """
            )
            # Se o processo caiu durante um envio, o lembrete continua elegível.
            connection.execute(
                "UPDATE lembretes_avulsos SET status = 'pendente' "
                "WHERE status = 'enviando'"
            )

    def add_personal_reminder(
        self, role: str, chat_id: int, text: str, reminder_at: datetime
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO lembretes_avulsos (
                    role, chat_id, texto, reminder_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    role,
                    chat_id,
                    text,
                    reminder_at.isoformat(),
                    datetime.now(self.timezone).isoformat(),
                ),
            )
            return int(cursor.lastrowid)

    def claim_due_personal_reminders(self, now: datetime) -> list[sqlite3.Row]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, role, chat_id, texto, reminder_at
                FROM lembretes_avulsos
                WHERE status = 'pendente' AND reminder_at <= ?
                ORDER BY reminder_at, id
                """,
                (now.isoformat(),),
            ).fetchall()
            if rows:
                connection.executemany(
                    "UPDATE lembretes_avulsos SET status = 'enviando' "
                    "WHERE id = ? AND status = 'pendente'",
                    [(row["id"],) for row in rows],
                )
            return rows

    def complete_personal_reminder(self, reminder_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM lembretes_avulsos WHERE id = ?", (reminder_id,)
            )

    def release_personal_reminder(self, reminder_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE lembretes_avulsos SET status = 'pendente' WHERE id = ?",
                (reminder_id,),
            )

    def conversation_history(
        self, role: str, limit: int = 4, max_age_days: int = 7
    ) -> list[dict]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT historico_json, historico_updated_at
                FROM conversas WHERE role = ?
                """,
                (role,),
            ).fetchone()
        if not row:
            return []
        try:
            updated_at = datetime.fromisoformat(row["historico_updated_at"])
        except (TypeError, ValueError):
            return []
        if datetime.now(self.timezone) - updated_at > timedelta(days=max_age_days):
            return []
        try:
            history = json.loads(row["historico_json"])
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(history, list):
            return []
        return [item for item in history if isinstance(item, dict)][-limit:]

    def save_conversation_turn(
        self,
        role: str,
        user_message: str,
        assistant_message: str,
        max_messages: int = 4,
    ) -> None:
        now = datetime.now(self.timezone).isoformat()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT historico_json, historico_updated_at
                FROM conversas WHERE role = ?
                """,
                (role,),
            ).fetchone()
            try:
                updated_at = (
                    datetime.fromisoformat(row["historico_updated_at"])
                    if row
                    else None
                )
                history = (
                    json.loads(row["historico_json"])
                    if updated_at
                    and datetime.now(self.timezone) - updated_at
                    <= timedelta(days=7)
                    else []
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                history = []
            if not isinstance(history, list):
                history = []
            history.extend(
                [
                    {"role": "user", "content": user_message[:1000]},
                    {"role": "assistant", "content": assistant_message[:2000]},
                ]
            )
            history = history[-max_messages:]
            connection.execute(
                """
                INSERT INTO conversas (
                    role, historico_json, historico_updated_at,
                    estado_json, estado_updated_at
                ) VALUES (?, ?, ?, '{}', ?)
                ON CONFLICT(role) DO UPDATE SET
                    historico_json = excluded.historico_json,
                    historico_updated_at = excluded.historico_updated_at
                """,
                (role, json.dumps(history, ensure_ascii=False), now, now),
            )

    def conversation_state(
        self, role: str, max_age_hours: int = 24
    ) -> dict:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT estado_json, estado_updated_at
                FROM conversas WHERE role = ?
                """,
                (role,),
            ).fetchone()
        if not row:
            return {}
        try:
            updated_at = datetime.fromisoformat(row["estado_updated_at"])
        except (TypeError, ValueError):
            return {}
        if datetime.now(self.timezone) - updated_at > timedelta(hours=max_age_hours):
            return {}
        try:
            state = json.loads(row["estado_json"])
        except (TypeError, json.JSONDecodeError):
            return {}
        return state if isinstance(state, dict) else {}

    def save_conversation_state(self, role: str, state: dict) -> None:
        now = datetime.now(self.timezone).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO conversas (
                    role, historico_json, historico_updated_at,
                    estado_json, estado_updated_at
                ) VALUES (?, '[]', ?, ?, ?)
                ON CONFLICT(role) DO UPDATE SET
                    estado_json = excluded.estado_json,
                    estado_updated_at = excluded.estado_updated_at
                """,
                (role, now, json.dumps(state, ensure_ascii=False), now),
            )

    def clear_conversation(self, role: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM conversas WHERE role = ?", (role,))

    def proactive_summary_sent(self, role: str, day: date, summary_type: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM resumos_proativos
                WHERE role = ? AND data = ? AND tipo = ?
                """,
                (role, day.isoformat(), summary_type),
            ).fetchone()
        return row is not None

    def mark_proactive_summary_sent(
        self, role: str, day: date, summary_type: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO resumos_proativos (role, data, tipo, sent_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    role,
                    day.isoformat(),
                    summary_type,
                    datetime.now(self.timezone).isoformat(),
                ),
            )

    def clean_old_proactive_summaries(self, retention_days: int = 90) -> None:
        limit = (datetime.now(self.timezone).date() - timedelta(days=retention_days))
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM resumos_proativos WHERE data < ?", (limit.isoformat(),)
            )

    def list_preferences(self, role: str) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT id, texto FROM preferencias WHERE role = ? ORDER BY id",
                (role,),
            ).fetchall()

    def add_preference(self, role: str, text: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO preferencias (role, texto, created_at) VALUES (?, ?, ?)",
                (role, text, datetime.now(self.timezone).isoformat()),
            )

    def remove_preference(self, role: str, index: int) -> bool:
        preferences = self.list_preferences(role)
        if index < 1 or index > len(preferences):
            return False
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM preferencias WHERE role = ? AND id = ?",
                (role, preferences[index - 1]["id"]),
            )
        return True

    def get_setting(self, role: str, key: str, default: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT valor FROM configuracoes WHERE role = ? AND chave = ?",
                (role, key),
            ).fetchone()
        return row["valor"] if row else default

    def save_setting(self, role: str, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO configuracoes (role, chave, valor) VALUES (?, ?, ?)
                ON CONFLICT(role, chave) DO UPDATE SET valor = excluded.valor
                """,
                (role, key, value),
            )

    def save_routine(self, role: str, name: str, events: list[dict]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO rotinas (role, nome, eventos_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(role, nome) DO UPDATE SET
                    eventos_json = excluded.eventos_json,
                    updated_at = excluded.updated_at
                """,
                (
                    role,
                    name,
                    json.dumps(events),
                    datetime.now(self.timezone).isoformat(),
                ),
            )

    def get_routine(self, role: str, name: str) -> Optional[list[dict]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT eventos_json FROM rotinas WHERE role = ? AND nome = ?",
                (role, name),
            ).fetchone()
        return json.loads(row["eventos_json"]) if row else None

    def list_routines(self, role: str) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT nome FROM rotinas WHERE role = ? ORDER BY nome", (role,)
            ).fetchall()
        return [row["nome"] for row in rows]

    def record_action(self, role: str, action_type: str, payload: dict) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO acoes (role, tipo, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    role,
                    action_type,
                    json.dumps(payload),
                    datetime.now(self.timezone).isoformat(),
                ),
            )
            limit = (datetime.now(self.timezone) - timedelta(days=90)).isoformat()
            connection.execute("DELETE FROM acoes WHERE created_at < ?", (limit,))

    def last_pending_action(self, role: str) -> Optional[sqlite3.Row]:
        limit = (datetime.now(self.timezone) - timedelta(hours=24)).isoformat()
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT * FROM acoes
                WHERE role = ? AND undone_at IS NULL AND created_at >= ?
                ORDER BY id DESC LIMIT 1
                """,
                (role, limit),
            ).fetchone()

    def mark_action_undone(self, action_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE acoes SET undone_at = ? WHERE id = ?",
                (datetime.now(self.timezone).isoformat(), action_id),
            )

    def user_role(self, telegram_user_id: int) -> Optional[str]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT role FROM usuarios WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
        return row["role"] if row else None

    def save_registration(
        self, role: str, telegram_user_id: int, chat_id: int
    ) -> tuple[bool, str]:
        with self.connect() as connection:
            role_owner = connection.execute(
                "SELECT telegram_user_id FROM usuarios WHERE role = ?", (role,)
            ).fetchone()
            id_role = connection.execute(
                "SELECT role FROM usuarios WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()

            if role_owner and role_owner["telegram_user_id"] != telegram_user_id:
                return False, f"O cadastro de {role} já pertence a outro usuário."
            if id_role and id_role["role"] != role:
                return False, f"Este Telegram já está cadastrado como {id_role['role']}."

            connection.execute(
                """
                INSERT INTO usuarios (role, telegram_user_id, chat_id, registered_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(role) DO UPDATE SET
                    telegram_user_id = excluded.telegram_user_id,
                    chat_id = excluded.chat_id,
                    registered_at = excluded.registered_at
                """,
                (
                    role,
                    telegram_user_id,
                    chat_id,
                    datetime.now(self.timezone).isoformat(),
                ),
            )
        return True, f"Cadastro concluído como {role}."

    def event_recipients(self, responsible: str) -> list[sqlite3.Row]:
        roles = ["Matheus", "Letícia"] if responsible == "Ambos" else [responsible]
        placeholders = ",".join("?" for _ in roles)
        with self.connect() as connection:
            return connection.execute(
                f"SELECT role, chat_id FROM usuarios WHERE role IN ({placeholders})",
                roles,
            ).fetchall()

    def notification_sent(self, event_id: str, start: str, chat_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM notificacoes
                WHERE event_id = ? AND start_time = ? AND chat_id = ?
                """,
                (event_id, start, chat_id),
            ).fetchone()
        return row is not None

    def mark_notification_sent(
        self,
        event_id: str,
        start: str,
        chat_id: int,
        reminder_minutes: Optional[int] = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO notificacoes
                    (event_id, start_time, chat_id, reminder_minutes, sent_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    start,
                    chat_id,
                    reminder_minutes or self.reminder_minutes,
                    datetime.now(self.timezone).isoformat(),
                ),
            )

    def clean_old_notifications(self) -> None:
        limit = (datetime.now(self.timezone) - timedelta(days=30)).isoformat()
        with self.connect() as connection:
            connection.execute("DELETE FROM notificacoes WHERE sent_at < ?", (limit,))
