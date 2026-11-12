"""Regras simples para lembretes avulsos, separados dos eventos da agenda."""

import re
from datetime import date, datetime, time, timedelta

from agenda_bot.intent_rules import texto_normalizado
from agenda_bot.settings import TZ

_WEEKDAYS = {
    "segunda": 0,
    "terca": 1,
    "quarta": 2,
    "quinta": 3,
    "sexta": 4,
    "sabado": 5,
    "domingo": 6,
}

_DAILY_PATTERN = re.compile(r"\b(?:todos?\s+os\s+dias|todo\s+dia|diariamente)\b")


def eh_pedido_lembrete_avulso(texto: str) -> bool:
    normalized = texto_normalizado(texto)
    return bool(
        re.search(
            r"\bme\s+(?:lembra|lembre|lembrar|avisa|avise|avisar)\b",
            normalized,
        )
        or re.search(
            r"\b(?:cria|crie|criar|adiciona|adicione|adicionar)\b.*\blembrete\b",
            normalized,
        )
        or re.match(r"\s*lembrete\s+(?:para|as|hoje|amanha)\b", normalized)
    )


def _parse_date(
    normalized: str, today: date
) -> tuple[date | None, tuple[int, int] | None]:
    match = re.search(
        r"\b(hoje|hj|amanha|segunda(?:-feira)?|terca(?:-feira)?|quarta(?:-feira)?|"
        r"quinta(?:-feira)?|sexta(?:-feira)?|sabado|domingo|"
        r"\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b",
        normalized,
    )
    if not match:
        return None, None
    value = match.group(1).replace("-feira", "")
    if value in {"hoje", "hj"}:
        result = today
    elif value == "amanha":
        result = today + timedelta(days=1)
    elif value in _WEEKDAYS:
        days_ahead = (_WEEKDAYS[value] - today.weekday()) % 7
        result = today + timedelta(days=days_ahead)
    else:
        day, month, *year = value.split("/")
        year_number = int(year[0]) if year else today.year
        if year and len(year[0]) == 2:
            year_number += 2000
        try:
            result = date(year_number, int(month), int(day))
        except ValueError:
            return None, match.span()
        if not year and result < today:
            result = result.replace(year=today.year + 1)
    return result, match.span()


def _parse_time(normalized: str) -> tuple[time | None, tuple[int, int] | None]:
    match = re.search(
        r"\bas\s*(\d{1,2})(?::(\d{2})|h\s*(\d{1,2})?)?\b|"
        r"\b(\d{1,2})(?::(\d{2})|h\s*(\d{1,2})?)\b",
        normalized,
    )
    if not match:
        return None, None
    end = match.end()
    suffix = re.match(r"\s*horas?\b", normalized[end:])
    if suffix:
        end += suffix.end()
    meridiem = re.match(
        r"\s*(?:(?:da|de)\s+)?(manha|tarde|noite)\b", normalized[end:]
    )
    if meridiem:
        end += meridiem.end()
    groups = match.groups()
    hour = int(groups[0] or groups[3])
    minute = int(groups[1] or groups[2] or groups[4] or groups[5] or 0)
    if meridiem:
        period = meridiem.group(1)
        if period in {"tarde", "noite"} and 1 <= hour < 12:
            hour += 12
        elif period == "manha" and hour == 12:
            hour = 0
    try:
        return time(hour, minute), (match.start(), end)
    except ValueError:
        return None, (match.start(), end)


def _mask_spans(text: str, spans: list[tuple[int, int]]) -> str:
    characters = list(text)
    for start, end in spans:
        characters[start:end] = " " * (end - start)
    return "".join(characters)


def _extract_items(text: str, spans: list[tuple[int, int]]) -> list[str]:
    remaining = _mask_spans(text, spans)
    normalized = texto_normalizado(remaining)
    prefix = re.match(
        r"\s*(?:(?:por favor|preciso|quero|pode)\s+)*(?:que\s+)?"
        r"(?:me\s+)?(?:lembre|lembra|lembrar|avise|avisa|avisar|lembrete)\b",
        normalized,
    )
    if prefix:
        remaining = (
            remaining[: prefix.start()]
            + " " * (prefix.end() - prefix.start())
            + remaining[prefix.end() :]
        )
        normalized = texto_normalizado(remaining)
    connector = re.match(r"\s*(?:de|do|da|dos|das|que|para|pra)\b\s*", normalized)
    if connector:
        remaining = (
            remaining[: connector.start()]
            + " " * (connector.end() - connector.start())
            + remaining[connector.end() :]
        )
    remaining = re.sub(r"^[\s,:;.!?\-]+", "", remaining)
    remaining = re.sub(r"[\s,:;.!?]+$", "", remaining)
    items = []
    for part in re.split(r"\s*[,;]\s*|\s+e\s+", remaining, flags=re.IGNORECASE):
        item = re.sub(r"^[\s,:;.!?\-]+|[\s,:;.!?]+$", "", part).strip()
        if item and texto_normalizado(item) not in {"me", "por favor"}:
            items.append(item[:180])
    return items[:10]


def parse_request(text: str, now: datetime | None = None) -> dict:
    """Extrai itens, data e hora sem IA; campos ausentes ficam como None."""
    now = now or datetime.now(TZ)
    normalized = texto_normalizado(text)
    recurrence_match = _DAILY_PATTERN.search(normalized)
    recurrence = "diaria" if recurrence_match else "nenhuma"
    due_time, time_span = _parse_time(normalized)
    # Em "todos os dias da Quarta Jovem", Quarta é parte do assunto, não um dia.
    # Datas explícitas continuam disponíveis para lembretes não recorrentes.
    if recurrence == "diaria":
        due_date, date_span = None, None
    else:
        due_date, date_span = _parse_date(normalized, now.date())
    recurrence_span = recurrence_match.span() if recurrence_match else None
    spans = [span for span in (time_span, date_span, recurrence_span) if span]
    items = _extract_items(text, spans)
    due_at = None
    if due_time and due_date:
        due_at = datetime.combine(due_date, due_time, tzinfo=TZ)
    return {
        "items": items,
        "date": due_date,
        "time": due_time,
        "due_at": due_at,
        "date_explicit": date_span is not None,
        "time_explicit": time_span is not None,
        "invalid_date": date_span is not None and due_date is None,
        "invalid_time": time_span is not None and due_time is None,
        "recurrence": recurrence,
    }


def apply_update(draft: dict, text: str, now: datetime | None = None) -> dict:
    """Aplica uma correção curta ao rascunho, ou interpreta uma nova versão completa."""
    now = now or datetime.now(TZ)
    normalized = texto_normalizado(text)
    update = parse_request(text, now)
    result = dict(draft)
    if update.get("recurrence") != "nenhuma":
        result["recurrence"] = update["recurrence"]
    if update["date"]:
        result["invalid_date"] = False
    elif update["invalid_date"]:
        result["invalid_date"] = True
    if update["time"]:
        result["invalid_time"] = False
    elif update["invalid_time"]:
        result["invalid_time"] = True
    if update["time"]:
        result["time"] = update["time"]
    if update["date"]:
        result["date"] = update["date"]
    if update["items"]:
        if re.search(r"\b(?:tira|remova|remover|exclui|excluir)\b", normalized):
            target_text = " ".join(update["items"])
            target_text = re.sub(
                r"^\s*(?:tira|remova|remover|exclui|excluir)\s+",
                "",
                target_text,
                flags=re.IGNORECASE,
            )
            target = texto_normalizado(target_text)
            result["items"] = [
                item
                for item in result.get("items", [])
                if target not in texto_normalizado(item)
            ]
        elif re.search(r"\b(?:adiciona|inclui|acrescenta|coloca)\b", normalized):
            additions = list(update["items"])
            additions[0] = re.sub(
                r"^\s*(?:adiciona|inclui|acrescenta|coloca)\s+",
                "",
                additions[0],
                flags=re.IGNORECASE,
            )
            result["items"] = result.get("items", []) + [
                item for item in additions if item
            ]
        elif update["time"] or update["date"]:
            # Uma correção de horário/data preserva os itens atuais.
            pass
        else:
            result["items"] = update["items"]
    if result.get("date") and result.get("time"):
        result["due_at"] = datetime.combine(result["date"], result["time"], tzinfo=TZ)
    else:
        result["due_at"] = None
    return result


def format_preview(draft: dict) -> str:
    lines = ["🔔 Confira este lembrete:"]
    lines.extend(f"• {item}" for item in draft.get("items", []))
    due_at = draft.get("due_at")
    if due_at:
        if draft.get("recurrence") == "diaria":
            lines.append(f"\n🔁 todos os dias às {due_at.strftime('%H:%M')}")
            return "\n".join(lines)
        today = datetime.now(TZ).date()
        if due_at.date() == today:
            day_label = "hoje"
        elif due_at.date() == today + timedelta(days=1):
            day_label = "amanhã"
        else:
            day_label = due_at.strftime("%d/%m/%Y")
        lines.append(f"\n🕒 {day_label} às {due_at.strftime('%H:%M')}")
    return "\n".join(lines)
