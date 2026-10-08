"""Operações do Google Agenda sem dependência de Telegram ou regras de conversa."""

import json
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from agenda_bot.event_rules import event_responsible, planned_event_description


def event_body(event: dict, timezone_name: str, colors: dict[str, str]) -> dict:
    description = event.get("description") or planned_event_description(event)
    return {
        "summary": event["titulo"],
        "description": description,
        "start": {"dateTime": event["inicio"].isoformat(), "timeZone": timezone_name},
        "end": {"dateTime": event["fim"].isoformat(), "timeZone": timezone_name},
        "colorId": colors.get(event["responsavel"], "2"),
    }


def update_event(
    event: dict,
    service_provider: Callable,
    calendar_id: str,
    timezone_name: str,
    colors: dict[str, str],
) -> None:
    service_provider().events().patch(
        calendarId=calendar_id,
        eventId=event["id"],
        body=event_body(event, timezone_name, colors),
    ).execute()


def event_exists(
    event: dict,
    service_provider: Callable,
    calendar_id: str,
    timezone: ZoneInfo,
) -> bool:
    response = (
        service_provider()
        .events()
        .list(
            calendarId=calendar_id,
            timeMin=event["inicio"].isoformat(),
            timeMax=event["fim"].isoformat(),
            singleEvents=True,
            q=event["titulo"],
            maxResults=10,
        )
        .execute()
    )
    for existing in response.get("items", []):
        if existing.get("summary") != event["titulo"]:
            continue
        existing_start = existing.get("start", {}).get("dateTime")
        if not existing_start:
            continue
        parsed_start = datetime.fromisoformat(
            existing_start.replace("Z", "+00:00")
        ).astimezone(timezone)
        if (
            parsed_start == event["inicio"]
            and event_responsible(existing) == event["responsavel"]
        ):
            return True
    return False


def create_event(
    event: dict,
    service_provider: Callable,
    calendar_id: str,
    timezone_name: str,
    timezone: ZoneInfo,
    colors: dict[str, str],
) -> bool:
    if event_exists(event, service_provider, calendar_id, timezone):
        return False
    service_provider().events().insert(
        calendarId=calendar_id,
        body=event_body(event, timezone_name, colors),
    ).execute()
    return True


def create_events_with_ids(
    events: list[dict],
    service_provider: Callable,
    calendar_id: str,
    timezone_name: str,
    timezone: ZoneInfo,
    colors: dict[str, str],
) -> list[str]:
    created_ids = []
    for event in events:
        if event_exists(event, service_provider, calendar_id, timezone):
            continue
        created = service_provider().events().insert(
            calendarId=calendar_id,
            body=event_body(event, timezone_name, colors),
        ).execute()
        if created.get("id"):
            created_ids.append(created["id"])
    return created_ids


def fetch_events(
    start: datetime,
    end: datetime,
    service_provider: Callable,
    calendar_id: str,
) -> list[dict]:
    response = (
        service_provider()
        .events()
        .list(
            calendarId=calendar_id,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        )
        .execute()
    )
    return response.get("items", [])


def fetch_events_to_cancel(
    term: Optional[str],
    start: datetime,
    end: datetime,
    service_provider: Callable,
    calendar_id: str,
) -> list[dict]:
    parameters = {
        "calendarId": calendar_id,
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 20,
    }
    if term:
        parameters["q"] = term
    response = service_provider().events().list(**parameters).execute()
    return response.get("items", [])


def delete_event(event_id: str, service_provider: Callable, calendar_id: str) -> None:
    service_provider().events().delete(
        calendarId=calendar_id, eventId=event_id
    ).execute()


def restoration_body(event: dict) -> dict:
    keys = (
        "summary",
        "description",
        "start",
        "end",
        "colorId",
        "location",
        "reminders",
    )
    return {key: event[key] for key in keys if key in event}


def undo_action(action, service_provider: Callable, calendar_id: str) -> str:
    payload = json.loads(action["payload_json"])
    service = service_provider().events()
    if action["tipo"] == "criacao":
        removed = 0
        for event_id in payload.get("event_ids", []):
            try:
                service.delete(calendarId=calendar_id, eventId=event_id).execute()
                removed += 1
            except HttpError as error:
                if getattr(error.resp, "status", None) != 404:
                    raise
        return f"{removed} evento(s) criado(s) foram removidos"
    event = payload["evento"]
    body = restoration_body(event)
    if action["tipo"] == "edicao":
        service.patch(
            calendarId=calendar_id, eventId=event["id"], body=body
        ).execute()
        return "o evento voltou ao estado anterior"
    if action["tipo"] == "cancelamento":
        service.insert(calendarId=calendar_id, body=body).execute()
        return "o evento cancelado foi recriado"
    raise ValueError("tipo de ação não suportado")
