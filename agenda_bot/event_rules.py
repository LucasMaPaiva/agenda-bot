"""Regras puras de eventos e disponibilidade, independentes do Telegram."""

import json
import re
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from agenda_bot.intent_rules import texto_normalizado
from agenda_bot.planning import TASK_MARKER, planned_task_ids, task_due_date


def event_start(event: dict, timezone: ZoneInfo) -> Optional[datetime]:
    value = event.get("start", {}).get("dateTime")
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone)


def event_end(event: dict, timezone: ZoneInfo) -> Optional[datetime]:
    value = event.get("end", {}).get("dateTime")
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone)


def event_responsible(event: dict) -> str:
    description = event.get("description", "")
    match = re.search(
        r"Responsável:\s*(Matheus|Letícia|Ambos)",
        description,
        flags=re.IGNORECASE,
    )
    if not match:
        return "Ambos"
    value = match.group(1).casefold()
    return {"matheus": "Matheus", "letícia": "Letícia", "ambos": "Ambos"}[value]


def event_summary(event: dict, timezone: ZoneInfo) -> str:
    title = event.get("summary", "Sem título")
    if len(title) > 80:
        title = f"{title[:77]}…"
    start = event_start(event, timezone)
    when = (
        start.strftime("%d/%m/%Y às %H:%M")
        if start
        else f"{event.get('start', {}).get('date', '')} (dia inteiro)"
    )
    return f"{title} — {when} ({event_responsible(event)})"


def format_schedule(events: list[dict], label: str, timezone: ZoneInfo) -> str:
    if not events:
        return f"📅 Você não tem compromissos para {label}."
    lines = [f"📅 Compromissos para {label}:"]
    for event in events[:30]:
        title = event.get("summary", "Sem título")
        if len(title) > 100:
            title = f"{title[:97]}…"
        start = event_start(event, timezone)
        end = event_end(event, timezone)
        if start:
            when = start.strftime("%d/%m, %H:%M")
            if end:
                when += f"–{end.strftime('%H:%M')}"
        else:
            when = f"{event.get('start', {}).get('date', '')} (dia inteiro)"
        lines.append(f"\n• {when} — {title} ({event_responsible(event)})")
    if len(events) > 30:
        lines.append(f"\n… e mais {len(events) - 30} compromissos.")
    return "".join(lines)


def filter_events_by_period(
    events: list[dict], period: str, timezone: ZoneInfo
) -> list[dict]:
    limits = {
        "madrugada": (0, 6),
        "manhã": (6, 12),
        "tarde": (12, 18),
        "noite": (18, 24),
    }
    if period not in limits:
        return events
    start_hour, end_hour = limits[period]
    found = []
    for event in events:
        start = event_start(event, timezone)
        end = event_end(event, timezone)
        if not start or not end:
            found.append(event)
            continue
        day = start.date()
        last_day = (end - timedelta(microseconds=1)).date()
        while day <= last_day:
            day_start = datetime.combine(day, datetime.min.time(), tzinfo=timezone)
            period_start = day_start + timedelta(hours=start_hour)
            period_end = day_start + timedelta(hours=end_hour)
            if start < period_end and period_start < end:
                found.append(event)
                break
            day += timedelta(days=1)
    return found


def filter_events_by_term(events: list[dict], term: str) -> list[dict]:
    normalized_term = texto_normalizado(term).strip()
    term_words = set(re.findall(r"[a-z0-9]+", normalized_term))
    if not term_words:
        return events
    found = []
    for event in events:
        normalized_title = texto_normalizado(event.get("summary", ""))
        title_words = set(re.findall(r"[a-z0-9]+", normalized_title))
        symbol_free_term = " ".join(sorted(term_words))
        symbol_free_title = " ".join(sorted(title_words))
        approximate_words = all(
            any(
                SequenceMatcher(None, word, title_word).ratio() >= 0.80
                for title_word in title_words
            )
            for word in term_words
        )
        approximate_title = (
            SequenceMatcher(None, symbol_free_term, symbol_free_title).ratio() >= 0.78
        )
        if (
            normalized_term in normalized_title
            or term_words <= title_words
            or approximate_words
            or approximate_title
        ):
            found.append(event)
    return found


def select_events_to_replicate(
    events: list[dict], terms: list[str], timezone: ZoneInfo
) -> list[dict]:
    if not terms:
        return events
    selected = {}
    missing = []
    for term in terms:
        found = filter_events_by_term(events, term)
        if not found:
            missing.append(term)
            continue
        for event in found:
            key = event.get("id") or (
                event.get("summary"),
                json.dumps(event.get("start", {}), sort_keys=True),
            )
            selected[key] = event
    if missing:
        available = ", ".join(
            dict.fromkeys(event.get("summary", "Sem título") for event in events)
        )
        suffix = f". Disponíveis no dia: {available}" if available else ""
        raise ValueError(
            "não encontrei no dia de origem: " + ", ".join(missing) + suffix
        )
    return sorted(
        selected.values(),
        key=lambda event: event_start(event, timezone)
        or datetime.max.replace(tzinfo=timezone),
    )


def prepare_replicated_events(
    events: list[dict], target_date: date, timezone: ZoneInfo
) -> list[dict]:
    copies = []
    for event in events:
        original_start = event_start(event, timezone)
        original_end = event_end(event, timezone)
        if not original_start or not original_end:
            raise ValueError(
                "a replicação de eventos de dia inteiro ainda não é suportada"
            )
        new_start = datetime.combine(
            target_date,
            original_start.time().replace(tzinfo=None),
            tzinfo=timezone,
        )
        copies.append(
            {
                "titulo": event.get("summary", "Sem título"),
                "responsavel": event_responsible(event),
                "inicio": new_start,
                "fim": new_start + (original_end - original_start),
                "recorrencia": "nenhuma",
            }
        )
    return copies


def replication_conflicts(
    new_events: list[dict],
    timezone: ZoneInfo,
    fetch_events: Callable[[datetime, datetime], list[dict]],
    ignored_ids: Optional[set[str]] = None,
) -> list[str]:
    if not new_events:
        return []
    search_start = min(event["inicio"] for event in new_events).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    search_end = (
        max(event["fim"] for event in new_events)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )
    conflicts = []
    ordered = sorted(new_events, key=lambda event: event["inicio"])
    for index, new in enumerate(ordered):
        for other in ordered[index + 1 :]:
            if other["inicio"] >= new["fim"]:
                break
            if new["inicio"] < other["fim"]:
                conflicts.append(
                    f"{new['titulo']} conflita com {other['titulo']} "
                    "entre os eventos copiados"
                )
    existing_events = fetch_events(search_start, search_end)
    for new in new_events:
        for existing in existing_events:
            if existing.get("id") in (ignored_ids or set()):
                continue
            existing_start = event_start(existing, timezone)
            existing_end = event_end(existing, timezone)
            if not existing_start or not existing_end:
                continue
            if new["inicio"] < existing_end and existing_start < new["fim"]:
                conflicts.append(
                    f"{new['titulo']} conflita com "
                    f"{existing.get('summary', 'Sem título')} "
                    f"({existing_start.strftime('%H:%M')}–"
                    f"{existing_end.strftime('%H:%M')})"
                )
    return conflicts


def prepare_edited_event(
    event: dict, changes: dict, timezone: ZoneInfo
) -> dict:
    current_start = event_start(event, timezone)
    current_end = event_end(event, timezone)
    if not current_start or not current_end:
        raise ValueError("A edição de eventos de dia inteiro ainda não é suportada.")
    new_date = (
        date.fromisoformat(changes["nova_data"])
        if changes.get("nova_data")
        else current_start.date()
    )
    new_start_time = (
        datetime.strptime(changes["nova_hora_inicio"], "%H:%M").time()
        if changes.get("nova_hora_inicio")
        else current_start.time().replace(tzinfo=None)
    )
    new_start = datetime.combine(new_date, new_start_time, tzinfo=timezone)
    if changes.get("nova_hora_fim"):
        new_end_time = datetime.strptime(changes["nova_hora_fim"], "%H:%M").time()
        new_end = datetime.combine(new_date, new_end_time, tzinfo=timezone)
        if new_end <= new_start:
            new_end += timedelta(days=1)
    else:
        new_end = new_start + (current_end - current_start)
    result = {
        "id": event["id"],
        "titulo": changes.get("novo_titulo")
        or event.get("summary", "Sem título"),
        "responsavel": changes.get("novo_responsavel")
        or event_responsible(event),
        "inicio": new_start,
        "fim": new_end,
    }
    task_ids = planned_task_ids([event])
    if task_ids:
        result["task_id"] = next(iter(task_ids))
    return result


def planned_event_description(event: dict) -> str:
    lines = [f"Responsável: {event['responsavel']}"]
    if event.get("task_id"):
        lines.append(f"{TASK_MARKER} {event['task_id']}")
    return "\n".join(lines)


def conflicts_between_events(events: list[dict], timezone: ZoneInfo) -> list[str]:
    timed = []
    for event in events:
        start = event_start(event, timezone)
        end = event_end(event, timezone)
        if start and end:
            timed.append((start, end, event.get("summary", "Sem título")))
    timed.sort(key=lambda item: item[0])
    conflicts = []
    for index, (start, end, title) in enumerate(timed):
        for other_start, other_end, other_title in timed[index + 1 :]:
            if other_start >= end:
                break
            minutes = int(
                (min(end, other_end) - max(start, other_start)).total_seconds() // 60
            )
            if minutes > 0:
                conflicts.append(
                    f"{title} conflita com {other_title} por {minutes} min"
                )
    return conflicts


def task_due_label(task: dict, target_date: date) -> str:
    due = task_due_date(task)
    if not due:
        return "sem prazo"
    if due < target_date:
        return f"atrasada desde {due.strftime('%d/%m')}"
    if due == target_date:
        return "vence nesse dia"
    return f"vence em {due.strftime('%d/%m')}"


def format_free_times(
    target_date: date,
    duration_minutes: int,
    windows: list[tuple[datetime, datetime]],
) -> str:
    if not windows:
        return (
            f"⏱ Não encontrei uma janela de {duration_minutes} min em "
            f"{target_date.strftime('%d/%m/%Y')}."
        )
    lines = [
        f"⏱ Horários livres em {target_date.strftime('%d/%m/%Y')} "
        f"para {duration_minutes} min:"
    ]
    lines.extend(
        f"\n• {start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
        for start, end in windows[:6]
    )
    return "".join(lines)


def calculate_free_times(
    role: str,
    target_date: date,
    duration_minutes: int,
    timezone: ZoneInfo,
    get_setting: Callable[[str, str, str], str],
    fetch_events: Callable[[datetime, datetime], list[dict]],
    existing_events: Optional[list[dict]] = None,
    now: Optional[datetime] = None,
) -> list[tuple[datetime, datetime]]:
    if duration_minutes < 15 or duration_minutes > 480:
        raise ValueError("a duração deve ficar entre 15 minutos e 8 horas")
    try:
        start_time = datetime.strptime(
            get_setting(role, "inicio_dia", "06:00"), "%H:%M"
        ).time()
        end_time = datetime.strptime(
            get_setting(role, "fim_dia", "22:00"), "%H:%M"
        ).time()
        buffer_minutes = int(get_setting(role, "buffer_minutos", "15"))
    except ValueError as error:
        raise ValueError("as preferências de horário estão inválidas") from error
    window_start = datetime.combine(target_date, start_time, tzinfo=timezone)
    window_end = datetime.combine(target_date, end_time, tzinfo=timezone)
    current = now or datetime.now(timezone)
    if target_date == current.date() and current > window_start:
        rounded = current.replace(second=0, microsecond=0)
        if current.second or current.microsecond:
            rounded += timedelta(minutes=1)
        remainder = rounded.minute % 15
        if remainder:
            rounded += timedelta(minutes=15 - remainder)
        window_start = rounded
    if window_start >= window_end:
        return []
    events = [
        event
        for event in (
            existing_events
            if existing_events is not None
            else fetch_events(window_start, window_end)
        )
        if event_responsible(event) in {role, "Ambos"}
    ]
    occupied = []
    for event in events:
        start = event_start(event, timezone)
        end = event_end(event, timezone)
        if start and end:
            occupied.append(
                (
                    max(window_start, start - timedelta(minutes=buffer_minutes)),
                    min(window_end, end + timedelta(minutes=buffer_minutes)),
                )
            )
    occupied.sort()
    merged = []
    for start, end in occupied:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    free = []
    cursor = window_start
    duration = timedelta(minutes=duration_minutes)
    for start, end in merged:
        if start - cursor >= duration:
            free.append((cursor, start))
        cursor = max(cursor, end)
    if window_end - cursor >= duration:
        free.append((cursor, window_end))
    return free
