"""Classificação determinística de lembretes, sem chamadas a modelos de IA."""

import re
from unicodedata import combining, normalize

REMINDER_DEFAULTS = {
    "normal": 30,
    "preparo": 60,
    "deslocamento": 45,
}

REMINDER_SETTING_KEYS = {
    "normal": "lembrete_normal_minutos",
    "preparo": "lembrete_preparo_minutos",
    "deslocamento": "lembrete_deslocamento_minutos",
}

PREPARATION_TERMS = {
    "academia",
    "aeroporto",
    "aula",
    "consulta",
    "culto",
    "dentista",
    "entrevista",
    "escola",
    "exame",
    "igreja",
    "medica",
    "medico",
    "prova",
    "reuniao",
    "treino",
    "viagem",
    "voo",
}


def _normalized_words(value: str) -> set[str]:
    normalized = "".join(
        character
        for character in normalize("NFD", value.casefold())
        if not combining(character)
    )
    return set(re.findall(r"[a-z0-9]+", normalized))


def reminder_kind(event: dict) -> str:
    if event.get("location", "").strip():
        return "deslocamento"
    content = f"{event.get('summary', '')} {event.get('description', '')}"
    if _normalized_words(content) & PREPARATION_TERMS:
        return "preparo"
    return "normal"


def reminder_heading(kind: str) -> str:
    return {
        "deslocamento": "🚗 Hora de se preparar para sair",
        "preparo": "🎒 Hora de se preparar",
        "normal": "⏰ Lembrete",
    }.get(kind, "⏰ Lembrete")
