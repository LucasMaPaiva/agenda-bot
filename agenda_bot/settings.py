"""Configuração da aplicação carregada a partir do ambiente."""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash")
GEMINI_TIMEOUT_SECONDS = int(os.environ.get("GEMINI_TIMEOUT_SECONDS", "30"))
OLLAMA_ENABLED = os.environ.get("OLLAMA_ENABLED", "false").casefold() in {
    "1",
    "true",
    "yes",
    "sim",
}
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:1b")
OLLAMA_TIMEOUT_SECONDS = int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "60"))
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "1024"))
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30s")
LLM_FALLBACK_TO_GEMINI = os.environ.get(
    "LLM_FALLBACK_TO_GEMINI", "true"
).casefold() in {"1", "true", "yes", "sim"}
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]
CALENDAR_ID = os.environ["CALENDAR_ID"]
TIMEZONE = os.environ.get("TIMEZONE", "America/Boa_Vista")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "data/agenda-bot.db")
REMINDER_MINUTES = int(os.environ.get("REMINDER_MINUTES", "30"))
REMINDER_CHECK_SECONDS = int(os.environ.get("REMINDER_CHECK_SECONDS", "60"))
MAX_EVENT_RANGE_DAYS = int(os.environ.get("MAX_EVENT_RANGE_DAYS", "31"))
DEFAULT_RECURRENCE_DAYS = int(os.environ.get("DEFAULT_RECURRENCE_DAYS", "90"))
MAX_RECURRENCE_OCCURRENCES = int(
    os.environ.get("MAX_RECURRENCE_OCCURRENCES", "100")
)
APP_VERSION = os.environ.get("APP_VERSION", "3.0.0")
BUILD_COMMIT = os.environ.get("BUILD_COMMIT", "não informado")
GOOGLE_TASKS_TOKEN_DIR = os.environ.get("GOOGLE_TASKS_TOKEN_DIR", "data")
MATHEUS_REGISTRATION_CODE = os.environ.get("MATHEUS_REGISTRATION_CODE", "")
LETICIA_REGISTRATION_CODE = os.environ.get("LETICIA_REGISTRATION_CODE", "")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "WARNING").upper()
FINANCE_API_URL = os.environ.get("FINANCE_API_URL", "").rstrip("/")
FINANCE_API_TIMEOUT_SECONDS = int(
    os.environ.get("FINANCE_API_TIMEOUT_SECONDS", "10")
)
# Segredo compartilhado com o financeiro (TELEGRAM_LINK_SECRET lá) para o
# vínculo por link; sem ele, /conectar_financeiro fica desligado.
FINANCE_LINK_SECRET = os.environ.get("FINANCE_LINK_SECRET", "")
FINANCE_API_TOKENS = {
    "Matheus": os.environ.get("FINANCE_API_TOKEN_MATHEUS", ""),
    "Letícia": os.environ.get("FINANCE_API_TOKEN_LETICIA", ""),
}

TZ = ZoneInfo(TIMEZONE)
STARTED_AT = datetime.now(TZ)

COLOR_BY_RESPONSAVEL = {
    "Matheus": "9",  # Blueberry / Azul
    "Letícia": "4",  # Flamingo / Rosa
    "Ambos": "2",  # Sage / Verde
}

REGISTRATION_CODES = {
    "Matheus": MATHEUS_REGISTRATION_CODE,
    "Letícia": LETICIA_REGISTRATION_CODE,
}

TASKS_SCOPES = ["https://www.googleapis.com/auth/tasks"]
