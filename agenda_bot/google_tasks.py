"""Integração com o Google Tarefas."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build


class GoogleTasksGateway:
    def __init__(self, token_dir: str, timezone: ZoneInfo, scopes: list[str]) -> None:
        self.token_dir = token_dir
        self.timezone = timezone
        self.scopes = scopes

    def token_path(self, role: str) -> Path:
        name = "matheus" if role == "Matheus" else "leticia"
        return Path(self.token_dir) / f"tasks-{name}.json"

    def service_for(self, role: str):
        path = self.token_path(role)
        if not path.is_file():
            raise ValueError(
                f"Google Tarefas ainda não foi conectado para {role}. "
                f"Gere o arquivo {path.name}."
            )
        credentials = UserCredentials.from_authorized_user_file(path, self.scopes)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            path.write_text(credentials.to_json(), encoding="utf-8")
            path.chmod(0o600)
        if not credentials.valid:
            raise ValueError(f"A autorização do Google Tarefas de {role} expirou.")
        return build(
            "tasks", "v1", credentials=credentials, cache_discovery=False
        )

    def create(self, role: str, task: dict) -> dict:
        body = {"title": task["titulo"]}
        if task.get("notas"):
            body["notes"] = task["notas"]
        if task.get("prazo"):
            body["due"] = f"{task['prazo']}T00:00:00.000Z"
        return (
            self.service_for(role)
            .tasks()
            .insert(tasklist="@default", body=body)
            .execute()
        )

    def list_pending(self, role: str) -> list[dict]:
        response = (
            self.service_for(role)
            .tasks()
            .list(
                tasklist="@default",
                showCompleted=False,
                showHidden=False,
                maxResults=50,
            )
            .execute()
        )
        tasks = response.get("items", [])
        return sorted(
            tasks, key=lambda item: (item.get("due", "9999"), item.get("title", ""))
        )

    def complete(self, role: str, task_id: str) -> None:
        self.service_for(role).tasks().patch(
            tasklist="@default",
            task=task_id,
            body={
                "status": "completed",
                "completed": datetime.now(self.timezone).isoformat(),
            },
        ).execute()

    def edit(self, role: str, task_id: str, title: str) -> None:
        self.service_for(role).tasks().patch(
            tasklist="@default", task=task_id, body={"title": title}
        ).execute()

    def delete(self, role: str, task_id: str) -> None:
        self.service_for(role).tasks().delete(
            tasklist="@default", task=task_id
        ).execute()
