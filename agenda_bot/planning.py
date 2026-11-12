"""Planejamento diário determinístico e leve, sem chamadas a modelos de IA."""

import re
from datetime import date, datetime, timedelta

TASK_MARKER = "Tarefa Google:"


def task_due_date(task: dict) -> date | None:
    value = task.get("due")
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def planned_task_ids(events: list[dict]) -> set[str]:
    result = set()
    for event in events:
        match = re.search(
            rf"^{re.escape(TASK_MARKER)}\s*(\S+)",
            event.get("description", ""),
            flags=re.MULTILINE,
        )
        if match:
            result.add(match.group(1))
    return result


def prioritize_tasks(
    tasks: list[dict],
    target_date: date,
    ignored_ids: set[str] | None = None,
    limit: int = 5,
) -> list[dict]:
    ignored = ignored_ids or set()
    candidates = []
    for position, task in enumerate(tasks):
        task_id = task.get("id")
        if task_id and task_id in ignored:
            continue
        due = task_due_date(task)
        if due and due > target_date:
            continue
        priority = 0 if due and due < target_date else 1 if due else 2
        candidates.append((priority, due or date.max, position, task))
    candidates.sort(key=lambda item: item[:3])
    return [item[3] for item in candidates[:limit]]


def allocate_task_blocks(
    tasks: list[dict],
    free_windows: list[tuple[datetime, datetime]],
    duration_minutes: int,
    responsible: str,
) -> tuple[list[dict], list[dict]]:
    duration = timedelta(minutes=duration_minutes)
    remaining = list(tasks)
    blocks = []
    for window_start, window_end in free_windows:
        cursor = window_start
        while remaining and cursor + duration <= window_end:
            task = remaining.pop(0)
            blocks.append(
                {
                    "titulo": f"Foco: {task.get('title', 'Tarefa sem título')}",
                    "inicio": cursor,
                    "fim": cursor + duration,
                    "responsavel": responsible,
                    "recorrencia": "nenhuma",
                    "task_id": task.get("id"),
                }
            )
            cursor += duration
        if not remaining:
            break
    return blocks, remaining
