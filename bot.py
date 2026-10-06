import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from unicodedata import combining, normalize
from zoneinfo import ZoneInfo

from google import genai
from google.auth.transport.requests import Request
from google.genai import errors, types
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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
APP_VERSION = os.environ.get("APP_VERSION", "2.1.0")
BUILD_COMMIT = os.environ.get("BUILD_COMMIT", "não informado")
GOOGLE_TASKS_TOKEN_DIR = os.environ.get("GOOGLE_TASKS_TOKEN_DIR", "data")
MATHEUS_REGISTRATION_CODE = os.environ.get("MATHEUS_REGISTRATION_CODE", "")
LETICIA_REGISTRATION_CODE = os.environ.get("LETICIA_REGISTRATION_CODE", "")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "WARNING").upper()
TZ = ZoneInfo(TIMEZONE)
STARTED_AT = datetime.now(TZ)
LOGGER = logging.getLogger(__name__)

# Mesma legenda de cor usada no colorizeRotina.gs (Documents/agenda/scripts)
COLOR_BY_RESPONSAVEL = {
    "Matheus": "9",   # Blueberry / Azul
    "Letícia": "4",   # Flamingo / Rosa
    "Ambos": "2",     # Sage / Verde
}

REGISTRATION_CODES = {
    "Matheus": MATHEUS_REGISTRATION_CODE,
    "Letícia": LETICIA_REGISTRATION_CODE,
}

gemini_client = (
    genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(
            timeout=GEMINI_TIMEOUT_SECONDS * 1000,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    if GEMINI_API_KEY
    else None
)

EVENTO_SCHEMA = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string"},
        "data": {"type": "string", "format": "date"},
        "data_fim": {
            "type": ["string", "null"],
            "format": "date",
            "description": "Último dia inclusivo de um intervalo ou null",
        },
        "hora_inicio": {"type": "string", "description": "Horário no formato HH:MM"},
        "hora_fim": {
            "type": ["string", "null"],
            "description": "Horário no formato HH:MM ou null",
        },
        "responsavel": {"type": "string", "enum": ["Matheus", "Letícia", "Ambos"]},
        "recorrencia": {
            "type": "string",
            "enum": ["nenhuma", "diaria", "semanal", "mensal"],
        },
        "dias_semana": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "segunda",
                    "terca",
                    "quarta",
                    "quinta",
                    "sexta",
                    "sabado",
                    "domingo",
                ],
            },
        },
        "ordem_mes": {
            "type": ["integer", "null"],
            "description": "1 a 5 ou -1 para o último dia da semana no mês",
        },
    },
    "required": [
        "titulo",
        "data",
        "data_fim",
        "hora_inicio",
        "hora_fim",
        "responsavel",
        "recorrencia",
        "dias_semana",
        "ordem_mes",
    ],
    "additionalProperties": False,
}

CANCELAMENTO_SCHEMA = {
    "type": "object",
    "properties": {
        "termo": {
            "type": ["string", "null"],
            "description": "Título ou palavras relevantes do evento, sem verbos de exclusão",
        },
        "data": {
            "type": ["string", "null"],
            "format": "date",
            "description": "Data inicial informada ou null",
        },
        "data_fim": {
            "type": ["string", "null"],
            "format": "date",
            "description": "Última data inclusiva do intervalo ou null",
        },
    },
    "required": ["termo", "data", "data_fim"],
    "additionalProperties": False,
}

EDICAO_SCHEMA = {
    "type": "object",
    "properties": {
        "termo": {
            "type": ["string", "null"],
            "description": "Título atual ou palavras relevantes do evento",
        },
        "data": {
            "type": ["string", "null"],
            "format": "date",
            "description": "Data atual usada para localizar o evento",
        },
        "novo_titulo": {"type": ["string", "null"]},
        "nova_data": {"type": ["string", "null"], "format": "date"},
        "nova_hora_inicio": {
            "type": ["string", "null"],
            "description": "Novo horário HH:MM ou null",
        },
        "nova_hora_fim": {
            "type": ["string", "null"],
            "description": "Novo horário HH:MM ou null",
        },
        "novo_responsavel": {
            "type": ["string", "null"],
            "enum": ["Matheus", "Letícia", "Ambos", None],
        },
    },
    "required": [
        "termo",
        "data",
        "novo_titulo",
        "nova_data",
        "nova_hora_inicio",
        "nova_hora_fim",
        "novo_responsavel",
    ],
    "additionalProperties": False,
}

TAREFA_SCHEMA = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string"},
        "notas": {"type": ["string", "null"]},
        "prazo": {"type": ["string", "null"], "format": "date"},
    },
    "required": ["titulo", "notas", "prazo"],
    "additionalProperties": False,
}

INTENCAO_SCHEMA = {
    "type": "object",
    "properties": {
        "intencao": {
            "type": "string",
            "enum": [
                "criar_evento",
                "consultar_agenda",
                "criar_tarefa",
                "listar_tarefas",
                "concluir_tarefa",
                "cancelar_evento",
                "editar_evento",
                "conversa",
            ],
        },
        "data": {"type": ["string", "null"], "format": "date"},
        "data_fim": {"type": ["string", "null"], "format": "date"},
        "pessoal": {"type": "boolean"},
        "numero_tarefa": {"type": ["integer", "null"]},
    },
    "required": [
        "intencao",
        "data",
        "data_fim",
        "pessoal",
        "numero_tarefa",
    ],
    "additionalProperties": False,
}

TASKS_SCOPES = ["https://www.googleapis.com/auth/tasks"]

calendar_service = None


def obter_calendar_service():
    global calendar_service
    if calendar_service is None:
        credentials = service_account.Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE,
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
        calendar_service = build("calendar", "v3", credentials=credentials)
    return calendar_service


def conectar_banco() -> sqlite3.Connection:
    conexao = sqlite3.connect(DATABASE_PATH)
    conexao.row_factory = sqlite3.Row
    return conexao


def inicializar_banco() -> None:
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    with conectar_banco() as conexao:
        conexao.executescript(
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
            """
        )


def role_do_usuario(telegram_user_id: int) -> Optional[str]:
    with conectar_banco() as conexao:
        linha = conexao.execute(
            "SELECT role FROM usuarios WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
    return linha["role"] if linha else None


def salvar_registro(role: str, telegram_user_id: int, chat_id: int) -> tuple[bool, str]:
    with conectar_banco() as conexao:
        dono_da_role = conexao.execute(
            "SELECT telegram_user_id FROM usuarios WHERE role = ?", (role,)
        ).fetchone()
        role_do_id = conexao.execute(
            "SELECT role FROM usuarios WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()

        if dono_da_role and dono_da_role["telegram_user_id"] != telegram_user_id:
            return False, f"O cadastro de {role} já pertence a outro usuário."
        if role_do_id and role_do_id["role"] != role:
            return False, f"Este Telegram já está cadastrado como {role_do_id['role']}."

        conexao.execute(
            """
            INSERT INTO usuarios (role, telegram_user_id, chat_id, registered_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(role) DO UPDATE SET
                telegram_user_id = excluded.telegram_user_id,
                chat_id = excluded.chat_id,
                registered_at = excluded.registered_at
            """,
            (role, telegram_user_id, chat_id, datetime.now(TZ).isoformat()),
        )
    return True, f"Cadastro concluído como {role}."


def destinatarios_do_evento(responsavel: str) -> list[sqlite3.Row]:
    roles = ["Matheus", "Letícia"] if responsavel == "Ambos" else [responsavel]
    placeholders = ",".join("?" for _ in roles)
    with conectar_banco() as conexao:
        return conexao.execute(
            f"SELECT role, chat_id FROM usuarios WHERE role IN ({placeholders})",
            roles,
        ).fetchall()


def notificacao_enviada(event_id: str, inicio: str, chat_id: int) -> bool:
    with conectar_banco() as conexao:
        linha = conexao.execute(
            """
            SELECT 1 FROM notificacoes
            WHERE event_id = ? AND start_time = ? AND chat_id = ?
              AND reminder_minutes = ?
            """,
            (event_id, inicio, chat_id, REMINDER_MINUTES),
        ).fetchone()
    return linha is not None


def marcar_notificacao_enviada(event_id: str, inicio: str, chat_id: int) -> None:
    with conectar_banco() as conexao:
        conexao.execute(
            """
            INSERT OR IGNORE INTO notificacoes
                (event_id, start_time, chat_id, reminder_minutes, sent_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, inicio, chat_id, REMINDER_MINUTES, datetime.now(TZ).isoformat()),
        )


def limpar_notificacoes_antigas() -> None:
    limite = (datetime.now(TZ) - timedelta(days=30)).isoformat()
    with conectar_banco() as conexao:
        conexao.execute("DELETE FROM notificacoes WHERE sent_at < ?", (limite,))


def gerar_resposta_ollama(
    mensagens: list[dict], schema: Optional[dict] = None, max_tokens: int = 512
) -> str:
    corpo = {
        "model": OLLAMA_MODEL,
        "messages": mensagens,
        "stream": False,
        "think": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {
            "temperature": 0 if schema else 0.5,
            "num_ctx": OLLAMA_NUM_CTX,
            "num_predict": max_tokens,
        },
    }
    if schema:
        corpo["format"] = schema
    resposta = requests.post(
        f"{OLLAMA_BASE_URL}/api/chat",
        json=corpo,
        timeout=OLLAMA_TIMEOUT_SECONDS,
    )
    resposta.raise_for_status()
    conteudo = resposta.json().get("message", {}).get("content", "").strip()
    if not conteudo:
        raise ValueError("o modelo local retornou uma resposta vazia")
    if schema:
        json.loads(conteudo)
    return conteudo


def gerar_resposta_gemini(prompt: str, schema: dict = EVENTO_SCHEMA):
    if gemini_client is None:
        raise RuntimeError("Gemini não está configurado")
    modelos = list(dict.fromkeys(filter(None, [GEMINI_MODEL, GEMINI_FALLBACK_MODEL])))
    config = {
        "response_mime_type": "application/json",
        "response_json_schema": schema,
        "automatic_function_calling": {"disable": True},
    }

    for indice, modelo in enumerate(modelos):
        try:
            return gemini_client.models.generate_content(
                model=modelo,
                contents=prompt,
                config=config,
            )
        except errors.ServerError as erro:
            ultimo_modelo = indice == len(modelos) - 1
            if erro.code != 503 or ultimo_modelo:
                raise
            LOGGER.warning(
                "Modelo Gemini %s indisponível; tentando %s",
                modelo,
                modelos[indice + 1],
            )

    raise RuntimeError("Nenhum modelo Gemini disponível")


def gerar_json_intencao(prompt: str, schema: dict) -> str:
    if OLLAMA_ENABLED:
        try:
            return gerar_resposta_ollama(
                [
                    {
                        "role": "system",
                        "content": (
                            "Responda em português do Brasil. Siga exatamente o "
                            "schema JSON solicitado, sem texto adicional."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                schema=schema,
                max_tokens=160,
            )
        except (
            requests.RequestException,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ) as erro:
            LOGGER.warning("Falha no modelo local %s: %s", OLLAMA_MODEL, erro)
            if not LLM_FALLBACK_TO_GEMINI:
                raise
    return gerar_resposta_gemini(prompt, schema).text


def gerar_json_operacao(prompt: str, schema: dict = EVENTO_SCHEMA) -> str:
    try:
        return gerar_resposta_gemini(prompt, schema).text
    except (errors.APIError, TimeoutError, RuntimeError) as erro:
        LOGGER.warning("Gemini indisponível; usando modelo local: %s", erro)
        if not OLLAMA_ENABLED:
            raise
        return gerar_resposta_ollama(
            [
                {
                    "role": "system",
                    "content": (
                        "Extraia os dados pedidos em português do Brasil. Siga "
                        "exatamente o schema JSON, sem texto adicional."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            schema=schema,
            max_tokens=512,
        )


def gerar_conversa_llm(mensagens: list[dict]) -> str:
    if OLLAMA_ENABLED:
        try:
            return gerar_resposta_ollama(mensagens, max_tokens=220)
        except (requests.RequestException, ValueError, KeyError) as erro:
            LOGGER.warning(
                "Falha na conversa com o modelo local %s: %s", OLLAMA_MODEL, erro
            )
            if not LLM_FALLBACK_TO_GEMINI:
                raise
    if gemini_client is None:
        raise RuntimeError("Nenhum modelo de conversa está disponível")
    resposta = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents="\n".join(
            f"{item['role']}: {item['content']}" for item in mensagens
        ),
    )
    return resposta.text.strip()


def extrair_eventos(texto: str) -> list[dict]:
    agora = datetime.now(TZ)
    prompt = f"""Você extrai dados de eventos de agenda a partir de mensagens em português do Brasil.
Data e hora atuais: {agora.strftime('%Y-%m-%d %H:%M')} ({TIMEZONE}), dia da semana: {agora.strftime('%A')}.

Regras:
- Resolva datas relativas ("hoje", "amanhã", nomes de dias da semana) com base na data atual acima.
- Para uma única data, preencha "data" e use null em "data_fim".
- Para um intervalo como "10/10 até 18/10", use o primeiro dia em "data" e o último em
  "data_fim". O intervalo inclui os dois dias e representa um evento por dia.
- Use recorrencia="diaria" para "todos os dias", "semanal" para dias da semana e
  "mensal" para expressões como "todo primeiro domingo do mês".
- Em recorrências semanais, liste os dias em "dias_semana". "De segunda a quinta"
  significa segunda, terça, quarta e quinta.
- Em recorrências mensais, use um dia em "dias_semana" e "ordem_mes" de 1 a 5;
  use -1 para "último". Nas demais recorrências, use null em "ordem_mes".
- Sem recorrência, use recorrencia="nenhuma" e uma lista vazia em "dias_semana".
- Se não der pra saber a hora de término, use null.
- Se não especificar responsável, use "Ambos".

Mensagem: "{texto}"
"""
    dados = json.loads(gerar_json_operacao(prompt))

    primeira_data = date.fromisoformat(dados["data"])
    ultima_data = date.fromisoformat(dados["data_fim"]) if dados.get("data_fim") else primeira_data
    if ultima_data < primeira_data:
        raise ValueError("A data final não pode ser anterior à data inicial.")

    recorrencia = dados.get("recorrencia", "nenhuma")
    if recorrencia != "nenhuma" and not dados.get("data_fim"):
        ultima_data = primeira_data + timedelta(days=DEFAULT_RECURRENCE_DAYS)

    quantidade_dias = (ultima_data - primeira_data).days + 1
    if recorrencia == "nenhuma" and quantidade_dias > MAX_EVENT_RANGE_DAYS:
        raise ValueError(
            f"O intervalo tem {quantidade_dias} dias; o máximo permitido é "
            f"{MAX_EVENT_RANGE_DAYS}."
        )

    indices_dias = {
        "segunda": 0,
        "terca": 1,
        "quarta": 2,
        "quinta": 3,
        "sexta": 4,
        "sabado": 5,
        "domingo": 6,
    }
    dias_semana = {
        indices_dias[dia]
        for dia in dados.get("dias_semana", [])
        if dia in indices_dias
    }
    if recorrencia in {"semanal", "mensal"} and not dias_semana:
        dias_semana = {primeira_data.weekday()}

    ordem_mes = dados.get("ordem_mes")
    if recorrencia == "mensal" and ordem_mes is None:
        ordem_mes = (primeira_data.day - 1) // 7 + 1

    datas_evento = []
    for deslocamento in range(quantidade_dias):
        data_evento = primeira_data + timedelta(days=deslocamento)
        incluir = recorrencia in {"nenhuma", "diaria"}
        if recorrencia == "semanal":
            incluir = data_evento.weekday() in dias_semana
        elif recorrencia == "mensal":
            mesmo_dia_semana = data_evento.weekday() in dias_semana
            if ordem_mes == -1:
                proxima_semana = data_evento + timedelta(days=7)
                posicao_correta = proxima_semana.month != data_evento.month
            else:
                posicao_correta = (data_evento.day - 1) // 7 + 1 == ordem_mes
            incluir = mesmo_dia_semana and posicao_correta
        if incluir:
            datas_evento.append(data_evento)

    if not datas_evento:
        raise ValueError("Nenhuma data corresponde à recorrência informada.")
    if len(datas_evento) > MAX_RECURRENCE_OCCURRENCES:
        raise ValueError(
            f"A recorrência gera {len(datas_evento)} eventos; o máximo permitido é "
            f"{MAX_RECURRENCE_OCCURRENCES}."
        )

    eventos = []
    for data_evento in datas_evento:
        inicio = datetime.strptime(
            f"{data_evento.isoformat()} {dados['hora_inicio']}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=TZ)
        if dados.get("hora_fim"):
            fim = datetime.strptime(
                f"{data_evento.isoformat()} {dados['hora_fim']}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=TZ)
        else:
            fim = inicio + timedelta(minutes=30)

        eventos.append(
            {
                "titulo": dados["titulo"],
                "responsavel": dados.get("responsavel", "Ambos"),
                "inicio": inicio,
                "fim": fim,
                "recorrencia": recorrencia,
            }
        )
    return eventos


def extrair_cancelamento(texto: str) -> tuple[Optional[str], datetime, datetime]:
    agora = datetime.now(TZ)
    prompt = f"""Você extrai critérios para localizar um evento que será cancelado no Google Agenda.
Data e hora atuais: {agora.strftime('%Y-%m-%d %H:%M')} ({TIMEZONE}).

Regras:
- Remova do termo verbos como cancelar, apagar, excluir, remover e desmarcar.
- Remova também palavras genéricas como evento, compromisso e tarefa quando não fizerem
  parte real do título. Se não sobrar um título ou assunto útil, use null em "termo".
- Resolva datas relativas como hoje e amanhã.
- Para uma data única, preencha "data" e use null em "data_fim".
- Para um intervalo, use a primeira data em "data" e a última em "data_fim".
- Se nenhuma data for informada, use null nos dois campos.

Mensagem: "{texto}"
"""
    dados = json.loads(gerar_json_operacao(prompt, CANCELAMENTO_SCHEMA))

    hoje = agora.replace(hour=0, minute=0, second=0, microsecond=0)
    if dados.get("data"):
        primeira_data = date.fromisoformat(dados["data"])
        ultima_data = (
            date.fromisoformat(dados["data_fim"])
            if dados.get("data_fim")
            else primeira_data
        )
        if ultima_data < primeira_data:
            raise ValueError("A data final não pode ser anterior à data inicial.")
        inicio = datetime.combine(primeira_data, datetime.min.time(), tzinfo=TZ)
        fim = datetime.combine(
            ultima_data + timedelta(days=1), datetime.min.time(), tzinfo=TZ
        )
    else:
        inicio = hoje
        fim = hoje + timedelta(days=MAX_EVENT_RANGE_DAYS)

    termo = dados.get("termo")
    return termo.strip() if termo and termo.strip() else None, inicio, fim


def extrair_edicao(
    texto: str,
) -> tuple[Optional[str], datetime, datetime, dict]:
    agora = datetime.now(TZ)
    prompt = f"""Você extrai critérios para localizar e editar um evento no Google Agenda.
Data e hora atuais: {agora.strftime('%Y-%m-%d %H:%M')} ({TIMEZONE}).

Regras:
- "termo" é o título atual do evento, sem verbos como mudar, editar ou reagendar.
- "data" é a data atual usada para localizar o evento. Se não for informada, use null.
- Campos iniciados por "nova" ou "novo" contêm somente as alterações solicitadas.
- Em "mude a academia de amanhã para 18h30", data é amanhã, nova_hora_inicio é
  18:30 e nova_data é null.
- Em "adie o culto de quarta para sexta", data é quarta e nova_data é sexta.
- Resolva datas relativas usando a data atual acima.
- Use null em todo campo que o usuário não pediu para alterar.

Mensagem: "{texto}"
"""
    dados = json.loads(gerar_json_operacao(prompt, EDICAO_SCHEMA))

    hoje = agora.replace(hour=0, minute=0, second=0, microsecond=0)
    if dados.get("data"):
        data_atual = date.fromisoformat(dados["data"])
        inicio = datetime.combine(data_atual, datetime.min.time(), tzinfo=TZ)
        fim = inicio + timedelta(days=1)
    else:
        inicio = hoje
        fim = hoje + timedelta(days=MAX_EVENT_RANGE_DAYS)

    alteracoes = {
        chave: dados.get(chave)
        for chave in (
            "novo_titulo",
            "nova_data",
            "nova_hora_inicio",
            "nova_hora_fim",
            "novo_responsavel",
        )
    }
    if not any(alteracoes.values()):
        raise ValueError("Não identifiquei qual alteração deve ser feita.")
    termo = dados.get("termo")
    return termo.strip() if termo and termo.strip() else None, inicio, fim, alteracoes


def preparar_evento_editado(evento: dict, alteracoes: dict) -> dict:
    inicio_atual = inicio_do_evento(evento)
    fim_atual = fim_do_evento(evento)
    if not inicio_atual or not fim_atual:
        raise ValueError("A edição de eventos de dia inteiro ainda não é suportada.")

    nova_data = (
        date.fromisoformat(alteracoes["nova_data"])
        if alteracoes.get("nova_data")
        else inicio_atual.date()
    )
    nova_hora_inicio = (
        datetime.strptime(alteracoes["nova_hora_inicio"], "%H:%M").time()
        if alteracoes.get("nova_hora_inicio")
        else inicio_atual.time().replace(tzinfo=None)
    )
    novo_inicio = datetime.combine(nova_data, nova_hora_inicio, tzinfo=TZ)

    if alteracoes.get("nova_hora_fim"):
        nova_hora_fim = datetime.strptime(
            alteracoes["nova_hora_fim"], "%H:%M"
        ).time()
        novo_fim = datetime.combine(nova_data, nova_hora_fim, tzinfo=TZ)
        if novo_fim <= novo_inicio:
            novo_fim += timedelta(days=1)
    else:
        novo_fim = novo_inicio + (fim_atual - inicio_atual)

    responsavel = alteracoes.get("novo_responsavel") or responsavel_do_evento(evento)
    return {
        "id": evento["id"],
        "titulo": alteracoes.get("novo_titulo") or evento.get("summary", "Sem título"),
        "responsavel": responsavel,
        "inicio": novo_inicio,
        "fim": novo_fim,
    }


def atualizar_evento_calendar(evento: dict) -> None:
    body = {
        "summary": evento["titulo"],
        "description": f"Responsável: {evento['responsavel']}",
        "start": {"dateTime": evento["inicio"].isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": evento["fim"].isoformat(), "timeZone": TIMEZONE},
        "colorId": COLOR_BY_RESPONSAVEL.get(evento["responsavel"], "2"),
    }
    obter_calendar_service().events().patch(
        calendarId=CALENDAR_ID, eventId=evento["id"], body=body
    ).execute()


def caminho_token_tasks(role: str) -> Path:
    nome = "matheus" if role == "Matheus" else "leticia"
    return Path(GOOGLE_TASKS_TOKEN_DIR) / f"tasks-{nome}.json"


def tasks_service_do_usuario(role: str):
    caminho = caminho_token_tasks(role)
    if not caminho.is_file():
        raise ValueError(
            f"Google Tarefas ainda não foi conectado para {role}. "
            f"Gere o arquivo {caminho.name}."
        )
    credenciais = UserCredentials.from_authorized_user_file(caminho, TASKS_SCOPES)
    if credenciais.expired and credenciais.refresh_token:
        credenciais.refresh(Request())
        caminho.write_text(credenciais.to_json(), encoding="utf-8")
        caminho.chmod(0o600)
    if not credenciais.valid:
        raise ValueError(f"A autorização do Google Tarefas de {role} expirou.")
    return build("tasks", "v1", credentials=credenciais, cache_discovery=False)


def extrair_tarefa(texto: str) -> dict:
    agora = datetime.now(TZ)
    prompt = f"""Você extrai uma tarefa pessoal do Google Tarefas.
Data atual: {agora.strftime('%Y-%m-%d')} ({TIMEZONE}).

Regras:
- "titulo" deve ser curto e não incluir o comando /tarefa.
- Resolva prazos relativos como hoje, amanhã, sexta ou semana que vem.
- O Google Tarefas guarda somente a data do prazo, sem horário.
- Se não houver prazo, use null. Se não houver notas, use null.

Mensagem: "{texto}"
"""
    return json.loads(gerar_json_operacao(prompt, TAREFA_SCHEMA))


def criar_tarefa_google(role: str, tarefa: dict) -> dict:
    body = {"title": tarefa["titulo"]}
    if tarefa.get("notas"):
        body["notes"] = tarefa["notas"]
    if tarefa.get("prazo"):
        body["due"] = f"{tarefa['prazo']}T00:00:00.000Z"
    return (
        tasks_service_do_usuario(role)
        .tasks()
        .insert(tasklist="@default", body=body)
        .execute()
    )


def listar_tarefas_google(role: str) -> list[dict]:
    resposta = (
        tasks_service_do_usuario(role)
        .tasks()
        .list(
            tasklist="@default",
            showCompleted=False,
            showHidden=False,
            maxResults=50,
        )
        .execute()
    )
    tarefas = resposta.get("items", [])
    return sorted(tarefas, key=lambda item: (item.get("due", "9999"), item.get("title", "")))


def concluir_tarefa_google(role: str, task_id: str) -> None:
    tasks_service_do_usuario(role).tasks().patch(
        tasklist="@default",
        task=task_id,
        body={"status": "completed", "completed": datetime.now(TZ).isoformat()},
    ).execute()


def evento_ja_existe(evento: dict) -> bool:
    resposta = (
        obter_calendar_service().events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=evento["inicio"].isoformat(),
            timeMax=evento["fim"].isoformat(),
            singleEvents=True,
            q=evento["titulo"],
            maxResults=10,
        )
        .execute()
    )
    for existente in resposta.get("items", []):
        if existente.get("summary") != evento["titulo"]:
            continue
        inicio_existente = existente.get("start", {}).get("dateTime")
        if not inicio_existente:
            continue
        inicio_existente = datetime.fromisoformat(
            inicio_existente.replace("Z", "+00:00")
        ).astimezone(TZ)
        if (
            inicio_existente == evento["inicio"]
            and responsavel_do_evento(existente) == evento["responsavel"]
        ):
            return True
    return False


def criar_evento_calendar(evento: dict) -> bool:
    if evento_ja_existe(evento):
        return False

    body = {
        "summary": evento["titulo"],
        "description": f"Responsável: {evento['responsavel']}",
        "start": {"dateTime": evento["inicio"].isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": evento["fim"].isoformat(), "timeZone": TIMEZONE},
        "colorId": COLOR_BY_RESPONSAVEL.get(evento["responsavel"], "2"),
    }
    obter_calendar_service().events().insert(
        calendarId=CALENDAR_ID, body=body
    ).execute()
    return True


def criar_eventos_calendar(eventos: list[dict]) -> int:
    return sum(criar_evento_calendar(evento) for evento in eventos)


def texto_normalizado(texto: str) -> str:
    return "".join(
        caractere
        for caractere in normalize("NFD", texto.casefold())
        if not combining(caractere)
    )


def eh_pedido_criacao_tarefa(texto: str) -> bool:
    palavras = set(re.findall(r"[a-z0-9]+", texto_normalizado(texto)))
    return bool(
        palavras & {"tarefa", "tarefas", "afazer", "afazeres"}
        and palavras
        & {
            "adiciona",
            "adicionar",
            "adicione",
            "anota",
            "anotar",
            "coloca",
            "colocar",
            "cria",
            "criar",
            "crie",
            "lembra",
            "lembrar",
        }
    )


def eh_pedido_criacao_evento(texto: str) -> bool:
    palavras = set(re.findall(r"[a-z0-9]+", texto_normalizado(texto)))
    verbos = palavras & {
        "adiciona",
        "adicionar",
        "adicione",
        "agenda",
        "agendar",
        "agende",
        "coloca",
        "colocar",
        "cria",
        "criar",
        "crie",
        "marca",
        "marcar",
        "marque",
    }
    sinais_agenda = palavras & {
        "agenda",
        "amanha",
        "compromisso",
        "compromissos",
        "domingo",
        "evento",
        "eventos",
        "hoje",
        "quarta",
        "quinta",
        "reuniao",
        "sabado",
        "segunda",
        "sexta",
        "terca",
    }
    tem_data_ou_hora = bool(
        re.search(
            r"\b(?:\d{1,2}(?:h\d{0,2}|:\d{2})|\d{1,2}/\d{1,2})\b",
            texto_normalizado(texto),
        )
    )
    return bool(
        verbos
        and (sinais_agenda or tem_data_ou_hora)
        and not eh_pedido_criacao_tarefa(texto)
    )


def classificar_intencao(texto: str) -> dict:
    agora = datetime.now(TZ)
    prompt = f"""Classifique a intenção de uma mensagem enviada a um assistente familiar.
Data e hora atuais: {agora.strftime('%Y-%m-%d %H:%M')} ({TIMEZONE}).

Intenções possíveis:
- criar_evento: marcar compromisso com data ou horário no Google Agenda.
- consultar_agenda: perguntar por compromissos/eventos.
- criar_tarefa: criar afazer ou lembrete pessoal no Google Tarefas.
- listar_tarefas: perguntar pelas tarefas ou afazeres pendentes.
- concluir_tarefa: concluir um número da última lista de tarefas.
- cancelar_evento: apagar, excluir ou desmarcar evento da agenda.
- editar_evento: mudar data, horário, título ou responsável de evento.
- conversa: saudação, pergunta geral, conselho ou conversa sem operação solicitada.

Regras:
- Não transforme conversa casual em evento.
- Só use criar_evento/criar_tarefa quando houver um pedido explícito para adicionar.
- Uma afirmação como "vou ter uma reunião quinta" ou um pedido de conselho sobre onde
  encaixar algo é conversa, não criação.
- Só use editar_evento ou cancelar_evento quando a alteração/exclusão for explícita.
- Para consulta de agenda, resolva o período e preencha data/data_fim. data_fim é inclusiva.
- "Minha agenda" ou "o que eu tenho" usa pessoal=true; "nossa", "ambos" ou
  "o que temos" usa pessoal=false.
- Para concluir tarefa, extraia o número em numero_tarefa.
- Nos outros casos use null nos campos que não se aplicam.

Mensagem: {json.dumps(texto, ensure_ascii=False)}
"""
    return json.loads(gerar_json_intencao(prompt, INTENCAO_SCHEMA))


def intervalo_da_intencao(dados: dict) -> tuple[datetime, datetime, str]:
    data_inicial = (
        date.fromisoformat(dados["data"])
        if dados.get("data")
        else datetime.now(TZ).date()
    )
    data_final = (
        date.fromisoformat(dados["data_fim"])
        if dados.get("data_fim")
        else data_inicial
    )
    if data_final < data_inicial:
        raise ValueError("a data final da consulta é anterior à inicial")
    if (data_final - data_inicial).days + 1 > MAX_EVENT_RANGE_DAYS:
        raise ValueError(
            f"a consulta pode abranger no máximo {MAX_EVENT_RANGE_DAYS} dias"
        )
    inicio = datetime.combine(data_inicial, datetime.min.time(), tzinfo=TZ)
    fim = datetime.combine(
        data_final + timedelta(days=1), datetime.min.time(), tzinfo=TZ
    )
    rotulo = (
        data_inicial.strftime("%d/%m/%Y")
        if data_inicial == data_final
        else f"{data_inicial.strftime('%d/%m')} a {data_final.strftime('%d/%m/%Y')}"
    )
    return inicio, fim, rotulo


def mensagem_erro_usuario(acao: str, erro: Exception) -> str:
    if isinstance(erro, errors.ServerError) and erro.code == 503:
        return (
            f"❌ Não consegui {acao}: o serviço de interpretação está ocupado. "
            "Tente novamente em alguns minutos."
        )
    if isinstance(erro, (TimeoutError, asyncio.TimeoutError)):
        return f"❌ Não consegui {acao}: a operação demorou demais. Tente novamente."
    if isinstance(erro, HttpError):
        status = getattr(erro.resp, "status", None)
        if status == 404:
            return f"❌ Não consegui {acao}: o evento não existe mais."
        if status == 403:
            return f"❌ Não consegui {acao}: o Google Agenda recusou a permissão."
    if isinstance(erro, ValueError):
        return f"❌ Não consegui {acao}: {erro}"
    return f"❌ Não consegui {acao} agora. Tente novamente."


def eh_pedido_cancelamento(texto: str) -> bool:
    palavras = set(re.findall(r"[a-z0-9]+", texto_normalizado(texto)))
    return bool(
        palavras
        & {
            "apaga",
            "apagar",
            "apague",
            "cancela",
            "cancelar",
            "cancele",
            "desmarca",
            "desmarcar",
            "desmarque",
            "exclua",
            "excluir",
            "exclui",
            "remove",
            "remover",
            "remova",
        }
    )


def eh_pedido_edicao(texto: str) -> bool:
    lista_palavras = re.findall(r"[a-z0-9]+", texto_normalizado(texto))
    if len(lista_palavras) > 1 and lista_palavras[:2] in (
        ["troca", "de"],
        ["troca", "da"],
        ["troca", "do"],
    ):
        return False
    palavras = set(lista_palavras)
    return bool(
        palavras
        & {
            "adia",
            "adiar",
            "adie",
            "altera",
            "alterar",
            "altere",
            "antecipa",
            "antecipar",
            "antecipe",
            "edita",
            "editar",
            "edite",
            "muda",
            "mudar",
            "mude",
            "reagenda",
            "reagendar",
            "reagende",
            "troca",
            "trocar",
            "troque",
        }
    )


def eh_consulta_agenda(texto: str) -> bool:
    palavras = re.findall(r"[a-z0-9]+", texto_normalizado(texto))
    conjunto = set(palavras)

    verbos_criacao = {
        "adiciona",
        "adicionar",
        "adicione",
        "agenda",
        "agendar",
        "agende",
        "cria",
        "criar",
        "crie",
        "marca",
        "marcar",
        "marque",
    }
    if conjunto & verbos_criacao:
        # "agenda" também é substantivo; só indica criação quando vem com um
        # complemento que não parece uma consulta.
        verbos_sem_agenda = verbos_criacao - {"agenda"}
        if conjunto & verbos_sem_agenda:
            return False
        if palavras[0] == "agenda" and len(palavras) > 2 and palavras[1] not in {
            "da",
            "de",
            "do",
            "hoje",
            "hj",
            "amanha",
            "pra",
            "para",
        }:
            return False

    verbos_listagem = {
        "consulta",
        "consultar",
        "liste",
        "listar",
        "mostra",
        "mostrar",
        "mostre",
        "ver",
        "veja",
    }
    termos_agenda = {"agenda", "agendas", "compromisso", "compromissos", "evento", "eventos"}
    if conjunto & verbos_listagem and conjunto & termos_agenda:
        return True

    verbos_posse = {"existe", "existem", "ha", "tenho", "tem", "temos", "ter"}
    interrogativos = {"oq", "oque", "q", "qual", "quais", "que"}
    if conjunto & verbos_posse and conjunto & interrogativos:
        return True

    periodos = {"hoje", "hj", "amanha", "semana"}
    plurais_agenda = {"agendas", "compromissos", "eventos"}
    possessivos = {"meu", "meus", "minha", "nossa", "nossas", "nosso", "nossos"}
    if "agenda" in conjunto and (conjunto & periodos or len(palavras) <= 3):
        return True
    if conjunto & plurais_agenda and conjunto & periodos:
        return True
    if conjunto & termos_agenda and conjunto & {"qual", "quais"}:
        return True
    if conjunto & termos_agenda and conjunto & possessivos:
        return True
    if "tem" in conjunto and conjunto & periodos:
        return True

    termos_genericos = {"algo", "alguma", "coisa"}
    if conjunto & termos_genericos and conjunto & periodos:
        return True

    palavras_de_consulta_curta = {
        "a",
        "da",
        "de",
        "do",
        "e",
        "essa",
        "esta",
        "hoje",
        "hj",
        "amanha",
        "o",
        "para",
        "pra",
        "que",
        "semana",
        "tem",
    }
    if conjunto & periodos and conjunto <= palavras_de_consulta_curta:
        return True

    return False


def consulta_eh_pessoal(texto: str) -> bool:
    palavras = set(re.findall(r"[a-z0-9]+", texto_normalizado(texto)))
    if palavras & {"ambos", "nossa", "nossas", "nosso", "nossos", "temos", "todos"}:
        return False
    return bool(palavras & {"eu", "meu", "meus", "minha", "minhas", "tenho"})


def intervalo_de_consulta(texto: str) -> Optional[tuple[datetime, datetime, str]]:
    normalizado = texto_normalizado(texto)
    if not eh_consulta_agenda(texto):
        return None

    hoje = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    if "amanha" in normalizado:
        inicio = hoje + timedelta(days=1)
        return inicio, inicio + timedelta(days=1), "amanhã"
    if "semana" in normalizado:
        dias_ate_proxima_segunda = 7 - hoje.weekday()
        return hoje, hoje + timedelta(days=dias_ate_proxima_segunda), "esta semana"
    tem_data_especifica = bool(
        re.search(r"\b\d{1,2}(?:/\d{1,2}(?:/\d{2,4})?)?\b", normalizado)
        or set(re.findall(r"[a-z]+", normalizado))
        & {
            "segunda",
            "terca",
            "quarta",
            "quinta",
            "sexta",
            "sabado",
            "domingo",
        }
    )
    if tem_data_especifica:
        # Datas livres e intervalos são resolvidos pelo classificador estruturado.
        return None
    return hoje, hoje + timedelta(days=1), "hoje"


def buscar_eventos(inicio: datetime, fim: datetime) -> list[dict]:
    resposta = (
        obter_calendar_service().events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=inicio.isoformat(),
            timeMax=fim.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        )
        .execute()
    )
    return resposta.get("items", [])


def buscar_eventos_para_cancelar(
    termo: Optional[str], inicio: datetime, fim: datetime
) -> list[dict]:
    parametros = {
        "calendarId": CALENDAR_ID,
        "timeMin": inicio.isoformat(),
        "timeMax": fim.isoformat(),
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 20,
    }
    if termo:
        parametros["q"] = termo
    resposta = obter_calendar_service().events().list(**parametros).execute()
    return resposta.get("items", [])


def excluir_evento_calendar(event_id: str) -> None:
    obter_calendar_service().events().delete(
        calendarId=CALENDAR_ID, eventId=event_id
    ).execute()


def inicio_do_evento(evento: dict) -> Optional[datetime]:
    valor = evento.get("start", {}).get("dateTime")
    if not valor:
        return None
    return datetime.fromisoformat(valor.replace("Z", "+00:00")).astimezone(TZ)


def fim_do_evento(evento: dict) -> Optional[datetime]:
    valor = evento.get("end", {}).get("dateTime")
    if not valor:
        return None
    return datetime.fromisoformat(valor.replace("Z", "+00:00")).astimezone(TZ)


def responsavel_do_evento(evento: dict) -> str:
    descricao = evento.get("description", "")
    correspondencia = re.search(
        r"Responsável:\s*(Matheus|Letícia|Ambos)", descricao, flags=re.IGNORECASE
    )
    if not correspondencia:
        return "Ambos"
    valor = correspondencia.group(1).casefold()
    return {"matheus": "Matheus", "letícia": "Letícia", "ambos": "Ambos"}[valor]


def resumo_evento(evento: dict) -> str:
    titulo = evento.get("summary", "Sem título")
    if len(titulo) > 80:
        titulo = f"{titulo[:77]}…"
    inicio = inicio_do_evento(evento)
    if inicio:
        horario = inicio.strftime("%d/%m/%Y às %H:%M")
    else:
        horario = f"{evento.get('start', {}).get('date', '')} (dia inteiro)"
    return f"{titulo} — {horario} ({responsavel_do_evento(evento)})"


def formatar_agenda(eventos: list[dict], rotulo: str) -> str:
    if not eventos:
        return f"📅 Você não tem compromissos para {rotulo}."

    linhas = [f"📅 Compromissos para {rotulo}:"]
    for evento in eventos[:30]:
        titulo = evento.get("summary", "Sem título")
        if len(titulo) > 100:
            titulo = f"{titulo[:97]}…"
        inicio = inicio_do_evento(evento)
        fim = fim_do_evento(evento)
        if inicio:
            horario = inicio.strftime("%d/%m, %H:%M")
            if fim:
                horario += f"–{fim.strftime('%H:%M')}"
        else:
            data = evento.get("start", {}).get("date", "")
            horario = f"{data} (dia inteiro)"
        linhas.append(f"\n• {horario} — {titulo} ({responsavel_do_evento(evento)})")

    if len(eventos) > 30:
        linhas.append(f"\n… e mais {len(eventos) - 30} compromissos.")
    return "".join(linhas)


async def responder_consulta(
    update: Update,
    intervalo: tuple[datetime, datetime, str],
    role: Optional[str] = None,
) -> None:
    inicio, fim, rotulo = intervalo
    eventos = await asyncio.to_thread(buscar_eventos, inicio, fim)
    if role:
        eventos = [
            evento
            for evento in eventos
            if responsavel_do_evento(evento) in {role, "Ambos"}
        ]
    await update.message.reply_text(formatar_agenda(eventos, rotulo))


def contexto_agenda_para_conversa(role: str) -> str:
    inicio = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    fim = inicio + timedelta(days=8)
    eventos = [
        evento
        for evento in buscar_eventos(inicio, fim)
        if responsavel_do_evento(evento) in {role, "Ambos"}
    ][:12]
    if not eventos:
        return "Agenda real dos próximos 8 dias: nenhum compromisso."

    linhas = ["Agenda real dos próximos 8 dias:"]
    eventos_com_horario = []
    for evento in eventos:
        comeco = inicio_do_evento(evento)
        termino = fim_do_evento(evento)
        titulo = evento.get("summary", "Sem título")
        if comeco and termino:
            linhas.append(
                f"- {comeco.strftime('%d/%m %H:%M')}–{termino.strftime('%H:%M')}: "
                f"{titulo} ({responsavel_do_evento(evento)})"
            )
            eventos_com_horario.append((comeco, termino, titulo))
        else:
            data_evento = evento.get("start", {}).get("date", "data não informada")
            linhas.append(f"- {data_evento}: {titulo} (dia inteiro)")

    conflitos = []
    eventos_com_horario.sort(key=lambda item: item[0])
    for indice, (comeco, termino, titulo) in enumerate(eventos_com_horario):
        for outro_comeco, outro_termino, outro_titulo in eventos_com_horario[
            indice + 1 :
        ]:
            if outro_comeco >= termino:
                break
            minutos = int(
                (min(termino, outro_termino) - max(comeco, outro_comeco)).total_seconds()
                // 60
            )
            if minutos > 0:
                conflitos.append(
                    f"- {titulo} conflita com {outro_titulo} por {minutos} min."
                )
    if conflitos:
        linhas.append("Conflitos calculados pelo sistema:")
        linhas.extend(conflitos[:5])
    else:
        linhas.append("Conflitos calculados pelo sistema: nenhum.")
    return "\n".join(linhas)


def salvar_acao_pendente(
    context: ContextTypes.DEFAULT_TYPE, chave: str, dados: dict
) -> str:
    agora = datetime.now(TZ).timestamp()
    pendentes = context.user_data.setdefault(chave, {})
    for token_antigo, pendente in list(pendentes.items()):
        if agora - pendente["criado_em"] > 900:
            del pendentes[token_antigo]
    while len(pendentes) >= 10:
        token_mais_antigo = min(
            pendentes, key=lambda token: pendentes[token]["criado_em"]
        )
        del pendentes[token_mais_antigo]
    token = secrets.token_hex(4)
    pendentes[token] = {"criado_em": agora, **dados}
    return token


def resumo_criacao(eventos: list[dict]) -> str:
    primeiro = eventos[0]
    if len(eventos) == 1:
        return (
            f"{primeiro['titulo']}\n"
            f"🗓 {primeiro['inicio'].strftime('%d/%m/%Y')}\n"
            f"🕒 {primeiro['inicio'].strftime('%H:%M')}–{primeiro['fim'].strftime('%H:%M')}\n"
            f"👤 {primeiro['responsavel']}"
        )
    return (
        f"{primeiro['titulo']}\n"
        f"🗓 {len(eventos)} eventos, de {primeiro['inicio'].strftime('%d/%m/%Y')} "
        f"a {eventos[-1]['inicio'].strftime('%d/%m/%Y')}\n"
        f"🕒 {primeiro['inicio'].strftime('%H:%M')}–{primeiro['fim'].strftime('%H:%M')}\n"
        f"👤 {primeiro['responsavel']}"
    )


async def iniciar_criacao(
    update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str
) -> None:
    eventos = await asyncio.to_thread(extrair_eventos, texto)
    token = salvar_acao_pendente(
        context, "criacoes_pendentes", {"eventos": eventos}
    )
    teclado = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Confirmar criação", callback_data=f"gc:{token}"
                )
            ],
            [InlineKeyboardButton("Não criar", callback_data=f"ga:{token}")],
        ]
    )
    await update.message.reply_text(
        f"Confira antes de criar:\n\n{resumo_criacao(eventos)}",
        reply_markup=teclado,
    )


async def callback_criacao(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not role_do_usuario(query.from_user.id):
        await query.answer("Telegram não autorizado.", show_alert=True)
        return
    await query.answer()

    acao, token = query.data.split(":", 1)
    pendentes = context.user_data.get("criacoes_pendentes", {})
    pendente = pendentes.get(token)
    if not pendente or datetime.now(TZ).timestamp() - pendente["criado_em"] > 900:
        pendentes.pop(token, None)
        await query.edit_message_text(
            "⌛ Esta confirmação expirou. Envie o evento novamente."
        )
        return
    if acao == "ga":
        pendentes.pop(token, None)
        await query.edit_message_text("👍 Criação cancelada. Nada foi adicionado.")
        return

    eventos = pendente["eventos"]
    try:
        quantidade_criada = await asyncio.to_thread(criar_eventos_calendar, eventos)
    except Exception:
        LOGGER.exception("Falha ao criar eventos confirmados")
        await query.edit_message_text(
            "❌ Não consegui criar o evento agora. Tente novamente em alguns minutos."
        )
        return

    pendentes.pop(token, None)
    quantidade_existente = len(eventos) - quantidade_criada
    mensagem = f"✅ {quantidade_criada} evento(s) criado(s)."
    if quantidade_existente:
        mensagem += f"\nℹ️ {quantidade_existente} já existia(m) e não foi(ram) duplicado(s)."
    await query.edit_message_text(f"{mensagem}\n\n{resumo_criacao(eventos)}")


def resumo_tarefa(tarefa: dict) -> str:
    prazo = tarefa.get("prazo")
    prazo_formatado = (
        date.fromisoformat(prazo).strftime("%d/%m/%Y") if prazo else "sem prazo"
    )
    texto = f"{tarefa['titulo']}\n📅 {prazo_formatado}"
    if tarefa.get("notas"):
        texto += f"\n📝 {tarefa['notas']}"
    return texto


async def iniciar_criacao_tarefa(
    update: Update, context: ContextTypes.DEFAULT_TYPE, role: str, texto: str
) -> None:
    tarefa_extraida = await asyncio.to_thread(extrair_tarefa, texto)
    token = salvar_acao_pendente(
        context,
        "tarefas_pendentes",
        {"role": role, "tarefa": tarefa_extraida},
    )
    teclado = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Criar tarefa", callback_data=f"tc:{token}")],
            [InlineKeyboardButton("Não criar", callback_data=f"ta:{token}")],
        ]
    )
    await update.message.reply_text(
        f"Confira a tarefa:\n\n{resumo_tarefa(tarefa_extraida)}",
        reply_markup=teclado,
    )


async def callback_tarefa(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    role = role_do_usuario(query.from_user.id)
    if not role:
        await query.answer("Telegram não autorizado.", show_alert=True)
        return
    await query.answer()

    acao, token = query.data.split(":", 1)
    pendentes = context.user_data.get("tarefas_pendentes", {})
    pendente = pendentes.get(token)
    if not pendente or datetime.now(TZ).timestamp() - pendente["criado_em"] > 900:
        pendentes.pop(token, None)
        await query.edit_message_text(
            "⌛ Esta confirmação expirou. Envie a tarefa novamente."
        )
        return
    if acao == "ta":
        pendentes.pop(token, None)
        await query.edit_message_text("👍 Criação cancelada. Nada foi adicionado.")
        return
    if pendente["role"] != role:
        await query.edit_message_text("❌ Esta tarefa pertence a outro usuário.")
        return

    try:
        await asyncio.to_thread(criar_tarefa_google, role, pendente["tarefa"])
    except Exception as erro:
        LOGGER.exception("Falha ao criar tarefa")
        await query.edit_message_text(
            mensagem_erro_usuario("criar a tarefa", erro)
        )
        return
    pendentes.pop(token, None)
    await query.edit_message_text(
        f"✅ Tarefa criada para {role}:\n\n{resumo_tarefa(pendente['tarefa'])}"
    )


def resumo_edicao(evento_original: dict, evento_novo: dict) -> str:
    return (
        f"Antes:\n{resumo_evento(evento_original)}\n\n"
        f"Depois:\n{evento_novo['titulo']} — "
        f"{evento_novo['inicio'].strftime('%d/%m/%Y às %H:%M')}–"
        f"{evento_novo['fim'].strftime('%H:%M')} "
        f"({evento_novo['responsavel']})"
    )


def teclado_confirmacao_edicao(token: str, indice: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Confirmar alteração", callback_data=f"ec:{token}:{indice}"
                )
            ],
            [InlineKeyboardButton("Não alterar", callback_data=f"ea:{token}")],
        ]
    )


async def iniciar_edicao(
    update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str
) -> None:
    termo, inicio, fim, alteracoes = await asyncio.to_thread(extrair_edicao, texto)
    eventos = await asyncio.to_thread(
        buscar_eventos_para_cancelar, termo, inicio, fim
    )
    if not eventos:
        await update.message.reply_text(
            "🔎 Não encontrei o evento para alterar. Informe o nome e a data atual."
        )
        return

    token = salvar_acao_pendente(
        context,
        "edicoes_pendentes",
        {"eventos": eventos, "alteracoes": alteracoes},
    )

    if len(eventos) == 1:
        evento_novo = preparar_evento_editado(eventos[0], alteracoes)
        await update.message.reply_text(
            resumo_edicao(eventos[0], evento_novo),
            reply_markup=teclado_confirmacao_edicao(token, 0),
        )
        return

    linhas = ["Encontrei estes eventos. Escolha qual deseja alterar:"]
    botoes = []
    for indice, evento in enumerate(eventos):
        linhas.append(f"\n{indice + 1}. {resumo_evento(evento)}")
        botoes.append(
            [
                InlineKeyboardButton(
                    f"{indice + 1}. {evento.get('summary', 'Sem título')}"[:60],
                    callback_data=f"es:{token}:{indice}",
                )
            ]
        )
    botoes.append(
        [InlineKeyboardButton("Não alterar", callback_data=f"ea:{token}")]
    )
    await update.message.reply_text(
        "".join(linhas), reply_markup=InlineKeyboardMarkup(botoes)
    )


async def callback_edicao(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not role_do_usuario(query.from_user.id):
        await query.answer("Telegram não autorizado.", show_alert=True)
        return
    await query.answer()

    partes = query.data.split(":")
    acao = partes[0]
    token = partes[1] if len(partes) > 1 else ""
    pendentes = context.user_data.get("edicoes_pendentes", {})
    pendente = pendentes.get(token)
    if not pendente or datetime.now(TZ).timestamp() - pendente["criado_em"] > 900:
        pendentes.pop(token, None)
        await query.edit_message_text(
            "⌛ Esta confirmação expirou. Envie o pedido de alteração novamente."
        )
        return
    if acao == "ea":
        pendentes.pop(token, None)
        await query.edit_message_text("👍 Alteração cancelada. Nada foi modificado.")
        return

    try:
        indice = int(partes[2])
        evento_original = pendente["eventos"][indice]
        evento_novo = preparar_evento_editado(
            evento_original, pendente["alteracoes"]
        )
    except (IndexError, KeyError, TypeError, ValueError):
        await query.edit_message_text("❌ Seleção ou alteração inválida.")
        return

    if acao == "es":
        await query.edit_message_text(
            resumo_edicao(evento_original, evento_novo),
            reply_markup=teclado_confirmacao_edicao(token, indice),
        )
        return

    if acao == "ec":
        try:
            await asyncio.to_thread(atualizar_evento_calendar, evento_novo)
        except Exception:
            LOGGER.exception("Falha ao editar evento")
            await query.edit_message_text(
                "❌ Não consegui alterar o evento agora. Tente novamente."
            )
            return
        pendentes.pop(token, None)
        await query.edit_message_text(
            f"✅ Evento alterado.\n\n{resumo_edicao(evento_original, evento_novo)}"
        )


async def iniciar_cancelamento(
    update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str
) -> None:
    termo, inicio, fim = await asyncio.to_thread(extrair_cancelamento, texto)
    eventos = await asyncio.to_thread(
        buscar_eventos_para_cancelar, termo, inicio, fim
    )
    if not eventos:
        await update.message.reply_text(
            "🔎 Não encontrei nenhum evento correspondente. Tente informar o nome e a data."
        )
        return

    token = salvar_acao_pendente(
        context, "cancelamentos_pendentes", {"eventos": eventos}
    )

    botao_desistir = InlineKeyboardButton(
        "Não cancelar", callback_data=f"ca:{token}"
    )
    if len(eventos) == 1:
        teclado = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Confirmar cancelamento", callback_data=f"cc:{token}:0"
                    )
                ],
                [botao_desistir],
            ]
        )
        await update.message.reply_text(
            f"Encontrei este evento:\n\n{resumo_evento(eventos[0])}\n\nDeseja cancelá-lo?",
            reply_markup=teclado,
        )
        return

    linhas = ["Encontrei estes eventos. Escolha qual deseja cancelar:"]
    botoes = []
    for indice, evento in enumerate(eventos):
        linhas.append(f"\n{indice + 1}. {resumo_evento(evento)}")
        titulo = evento.get("summary", "Sem título")
        botoes.append(
            [
                InlineKeyboardButton(
                    f"{indice + 1}. {titulo}"[:60],
                    callback_data=f"cs:{token}:{indice}",
                )
            ]
        )
    botoes.append([botao_desistir])
    await update.message.reply_text(
        "".join(linhas), reply_markup=InlineKeyboardMarkup(botoes)
    )


async def callback_cancelamento(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not role_do_usuario(query.from_user.id):
        await query.answer("Telegram não autorizado.", show_alert=True)
        return
    await query.answer()

    partes = query.data.split(":")
    acao = partes[0]
    token = partes[1] if len(partes) > 1 else ""
    pendentes = context.user_data.get("cancelamentos_pendentes", {})
    pendente = pendentes.get(token)
    if not pendente or datetime.now(TZ).timestamp() - pendente["criado_em"] > 900:
        pendentes.pop(token, None)
        await query.edit_message_text(
            "⌛ Esta confirmação expirou. Envie o pedido de cancelamento novamente."
        )
        return

    if acao == "ca":
        pendentes.pop(token, None)
        await query.edit_message_text("👍 Cancelamento desfeito. Nada foi apagado.")
        return

    try:
        indice = int(partes[2])
        evento = pendente["eventos"][indice]
    except (IndexError, TypeError, ValueError):
        await query.edit_message_text("❌ Seleção inválida ou expirada.")
        return

    if acao == "cs":
        teclado = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Confirmar cancelamento", callback_data=f"cc:{token}:{indice}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Não cancelar", callback_data=f"ca:{token}"
                    )
                ],
            ]
        )
        await query.edit_message_text(
            f"Você selecionou:\n\n{resumo_evento(evento)}\n\nDeseja realmente cancelar?",
            reply_markup=teclado,
        )
        return


    if acao == "cc":
        try:
            await asyncio.to_thread(excluir_evento_calendar, evento["id"])
        except Exception as erro:
            LOGGER.exception("Falha ao cancelar evento")
            await query.edit_message_text(
                mensagem_erro_usuario("cancelar o evento", erro)
            )
            return
        pendentes.pop(token, None)
        await query.edit_message_text(f"✅ Evento cancelado:\n\n{resumo_evento(evento)}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = role_do_usuario(update.effective_user.id)
    if role:
        mensagem = (
            f"Olá, {role}! Posso conversar e cuidar da agenda e das tarefas.\n"
            '"O que tenho hoje?"\n"Quais são meus compromissos de amanhã?"\n'
            '"Muda a academia de amanhã para 18h30"\n'
            '"Cancelar academia de amanhã"\n\n'
            "Comandos: /agenda, /editar, /cancelar, /tarefa, /tarefas, "
            "/concluir, /limpar_conversa e /versao"
        )
    else:
        mensagem = (
            "Este Telegram ainda não está autorizado. Use:\n"
            "/registrar matheus SEU_CODIGO\n"
            "ou\n"
            "/registrar leticia SEU_CODIGO"
        )
    await update.message.reply_text(mensagem)


async def registrar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.message.reply_text("Faça o cadastro em uma conversa privada com o bot.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Use: /registrar matheus CODIGO")
        return

    nomes = {"matheus": "Matheus", "leticia": "Letícia", "letícia": "Letícia"}
    role = nomes.get(context.args[0].casefold())
    if not role:
        await update.message.reply_text("Nome inválido. Use matheus ou leticia.")
        return

    codigo_esperado = REGISTRATION_CODES[role]
    if not codigo_esperado:
        await update.message.reply_text("O código de cadastro ainda não foi configurado.")
        return
    if not hmac.compare_digest(context.args[1], codigo_esperado):
        await update.message.reply_text("Código de cadastro inválido.")
        return

    sucesso, mensagem = salvar_registro(
        role, update.effective_user.id, update.effective_chat.id
    )
    prefixo = "✅" if sucesso else "❌"
    await update.message.reply_text(f"{prefixo} {mensagem}")


async def agenda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    texto = " ".join(context.args) if context.args else "minha agenda hoje"
    intervalo = intervalo_de_consulta(f"mostrar agenda {texto}")
    try:
        await responder_consulta(
            update, intervalo, role if consulta_eh_pessoal(texto) else None
        )
    except Exception as erro:
        LOGGER.exception("Falha ao consultar a agenda")
        await update.message.reply_text(
            mensagem_erro_usuario("consultar a agenda", erro)
        )


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not role_do_usuario(update.effective_user.id):
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    if not context.args:
        await update.message.reply_text(
            "Use: /cancelar NOME E DATA\nExemplo: /cancelar academia amanhã"
        )
        return
    try:
        await iniciar_cancelamento(update, context, " ".join(context.args))
    except Exception as erro:
        LOGGER.exception("Falha ao buscar evento para cancelamento")
        await update.message.reply_text(
            mensagem_erro_usuario("buscar o evento para cancelar", erro)
        )


async def editar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not role_do_usuario(update.effective_user.id):
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    if not context.args:
        await update.message.reply_text(
            "Use: /editar EVENTO E ALTERAÇÃO\n"
            "Exemplo: /editar academia de amanhã para 18h30"
        )
        return
    try:
        await iniciar_edicao(update, context, " ".join(context.args))
    except Exception as erro:
        LOGGER.exception("Falha ao buscar evento para edição")
        await update.message.reply_text(
            mensagem_erro_usuario("buscar o evento para alterar", erro)
        )


async def tarefa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    if not context.args:
        await update.message.reply_text(
            "Use: /tarefa DESCRIÇÃO\nExemplo: /tarefa comprar sabão até sexta"
        )
        return
    try:
        await iniciar_criacao_tarefa(update, context, role, " ".join(context.args))
    except Exception as erro:
        LOGGER.exception("Falha ao preparar tarefa")
        await update.message.reply_text(
            mensagem_erro_usuario("preparar a tarefa", erro)
        )


async def tarefas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    try:
        itens = await asyncio.to_thread(listar_tarefas_google, role)
    except Exception as erro:
        LOGGER.exception("Falha ao listar tarefas")
        await update.message.reply_text(
            mensagem_erro_usuario("listar suas tarefas", erro)
        )
        return
    if not itens:
        context.user_data["ultima_lista_tarefas"] = []
        await update.message.reply_text("✅ Você não tem tarefas pendentes.")
        return

    itens = itens[:30]
    context.user_data["ultima_lista_tarefas"] = [item["id"] for item in itens]
    linhas = [f"📋 Tarefas de {role}:"]
    for indice, item in enumerate(itens, start=1):
        prazo = item.get("due")
        prazo_texto = (
            datetime.fromisoformat(prazo.replace("Z", "+00:00")).strftime("%d/%m")
            if prazo
            else "sem prazo"
        )
        titulo = item.get("title", "Sem título")
        if len(titulo) > 90:
            titulo = f"{titulo[:87]}…"
        linhas.append(f"\n{indice}. {titulo} — {prazo_texto}")
    linhas.append("\n\nPara concluir: /concluir NÚMERO")
    await update.message.reply_text("".join(linhas))


async def concluir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text(
            "Primeiro use /tarefas e depois /concluir NÚMERO."
        )
        return
    await concluir_tarefa_da_lista(update, context, role, int(context.args[0]))


async def concluir_tarefa_da_lista(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    role: str,
    numero: int,
) -> None:
    indice = numero - 1
    ids = context.user_data.get("ultima_lista_tarefas", [])
    if indice < 0 or indice >= len(ids):
        await update.message.reply_text("Número inválido. Use /tarefas novamente.")
        return
    try:
        await asyncio.to_thread(concluir_tarefa_google, role, ids[indice])
    except Exception as erro:
        LOGGER.exception("Falha ao concluir tarefa")
        await update.message.reply_text(
            mensagem_erro_usuario("concluir a tarefa", erro)
        )
        return
    await update.message.reply_text("✅ Tarefa concluída.")


async def limpar_conversa(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not role_do_usuario(update.effective_user.id):
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    context.user_data.pop("historico_conversa", None)
    await update.message.reply_text("🧹 Histórico da conversa limpo.")


async def responder_conversa(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    role: str,
    texto: str,
) -> None:
    historico = context.user_data.setdefault("historico_conversa", [])
    agora = datetime.now(TZ)
    try:
        contexto_agenda = await asyncio.to_thread(contexto_agenda_para_conversa, role)
    except Exception:
        LOGGER.exception("Falha ao carregar contexto da agenda para conversa")
        contexto_agenda = "A agenda não pôde ser consultada neste momento."
    mensagens = [
        {
            "role": "system",
            "content": (
                "Você é o assistente da família Paiva. Converse em português do Brasil, "
                "com simpatia e respostas curtas e práticas. O usuário atual é "
                f"{role}; agora é {agora.strftime('%d/%m/%Y %H:%M')} em {TIMEZONE}. "
                "Use somente os compromissos e conflitos informados pelo sistema; não "
                "invente eventos. Se houver conflito, respeite prioridades ditas pelo "
                "usuário e sugira mover um compromisso para depois do término do outro. "
                "Você não executa ações diretamente e nunca deve afirmar que criou, "
                "alterou ou apagou algo. Pedidos de agenda e tarefas são executados "
                "separadamente pelo bot e sempre confirmados quando alteram dados.\n\n"
                f"{contexto_agenda}"
            ),
        },
        *historico[-4:],
        {"role": "user", "content": texto},
    ]
    resposta = await asyncio.to_thread(gerar_conversa_llm, mensagens)
    historico.extend(
        [
            {"role": "user", "content": texto[:1000]},
            {"role": "assistant", "content": resposta[:2000]},
        ]
    )
    del historico[:-4]
    await update.message.reply_text(resposta)


async def versao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not role_do_usuario(update.effective_user.id):
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    tempo_ativo = datetime.now(TZ) - STARTED_AT
    horas, resto = divmod(int(tempo_ativo.total_seconds()), 3600)
    minutos = resto // 60
    await update.message.reply_text(
        f"🤖 Agenda Bot v{APP_VERSION}\n"
        f"Commit: {BUILD_COMMIT}\n"
        f"Modelo: {OLLAMA_MODEL if OLLAMA_ENABLED else GEMINI_MODEL}\n"
        f"Fallback Gemini: {'sim' if LLM_FALLBACK_TO_GEMINI and GEMINI_API_KEY else 'não'}\n"
        f"Ativo há: {horas}h {minutos}min"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    texto = update.message.text
    role = role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return

    try:
        if eh_pedido_cancelamento(texto):
            await iniciar_cancelamento(update, context, texto)
            return

        if eh_pedido_edicao(texto):
            await iniciar_edicao(update, context, texto)
            return

        intervalo = intervalo_de_consulta(texto)
        if intervalo:
            await responder_consulta(
                update, intervalo, role if consulta_eh_pessoal(texto) else None
            )
            return

        if eh_pedido_criacao_tarefa(texto):
            await iniciar_criacao_tarefa(update, context, role, texto)
            return

        if eh_pedido_criacao_evento(texto):
            await iniciar_criacao(update, context, texto)
            return

        dados = await asyncio.to_thread(classificar_intencao, texto)
        intencao = dados["intencao"]
        if intencao == "criar_evento":
            await iniciar_criacao(update, context, texto)
        elif intencao == "consultar_agenda":
            await responder_consulta(
                update,
                intervalo_da_intencao(dados),
                role if dados.get("pessoal") else None,
            )
        elif intencao == "criar_tarefa":
            await iniciar_criacao_tarefa(update, context, role, texto)
        elif intencao == "listar_tarefas":
            await tarefas(update, context)
        elif intencao == "concluir_tarefa":
            numero = dados.get("numero_tarefa")
            if not numero:
                await update.message.reply_text(
                    "Qual número você quer concluir? Use /tarefas para ver a lista."
                )
            else:
                await concluir_tarefa_da_lista(update, context, role, numero)
        elif intencao == "cancelar_evento":
            await iniciar_cancelamento(update, context, texto)
        elif intencao == "editar_evento":
            await iniciar_edicao(update, context, texto)
        else:
            await responder_conversa(update, context, role, texto)
    except Exception as erro:
        LOGGER.exception("Falha ao processar mensagem")
        await update.message.reply_text(
            mensagem_erro_usuario("processar a mensagem", erro)
        )


async def verificar_lembretes(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        agora = datetime.now(TZ)
        fim_da_janela = agora + timedelta(minutes=REMINDER_MINUTES)
        eventos = buscar_eventos(agora, fim_da_janela)

        for evento in eventos:
            inicio = inicio_do_evento(evento)
            if not inicio or inicio < agora:
                continue
            inicio_iso = inicio.isoformat()
            responsavel = responsavel_do_evento(evento)
            minutos = max(0, int((inicio - agora).total_seconds() // 60))
            titulo = evento.get("summary", "Sem título")
            mensagem = (
                f"⏰ Lembrete: compromisso em {minutos} min\n"
                f"📅 {titulo}\n"
                f"🕒 {inicio.strftime('%d/%m às %H:%M')}\n"
                f"👤 {responsavel}"
            )

            for usuario in destinatarios_do_evento(responsavel):
                chat_id = usuario["chat_id"]
                if notificacao_enviada(evento["id"], inicio_iso, chat_id):
                    continue
                await context.bot.send_message(chat_id=chat_id, text=mensagem)
                marcar_notificacao_enviada(evento["id"], inicio_iso, chat_id)

        limpar_notificacoes_antigas()
    except Exception:
        LOGGER.exception("Falha ao verificar lembretes")


async def configurar_jobs(application: Application) -> None:
    if application.job_queue is None:
        raise RuntimeError('Instale a dependência "python-telegram-bot[job-queue]"')
    application.job_queue.run_repeating(
        verificar_lembretes,
        interval=REMINDER_CHECK_SECONDS,
        first=5,
        name="lembretes-agenda",
    )


def main() -> None:
    logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.WARNING))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    inicializar_banco()
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(configurar_jobs)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("registrar", registrar))
    app.add_handler(CommandHandler("agenda", agenda))
    app.add_handler(CommandHandler("cancelar", cancelar))
    app.add_handler(CommandHandler("editar", editar))
    app.add_handler(CommandHandler("tarefa", tarefa))
    app.add_handler(CommandHandler("tarefas", tarefas))
    app.add_handler(CommandHandler("concluir", concluir))
    app.add_handler(CommandHandler("limpar_conversa", limpar_conversa))
    app.add_handler(CommandHandler("versao", versao))
    app.add_handler(CallbackQueryHandler(callback_criacao, pattern=r"^g[ca]:"))
    app.add_handler(CallbackQueryHandler(callback_tarefa, pattern=r"^t[ca]:"))
    app.add_handler(CallbackQueryHandler(callback_edicao, pattern=r"^e[sca]:"))
    app.add_handler(
        CallbackQueryHandler(callback_cancelamento, pattern=r"^c[sca]:")
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
