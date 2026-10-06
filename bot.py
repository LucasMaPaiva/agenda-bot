import hmac
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from unicodedata import combining, normalize
from zoneinfo import ZoneInfo

from google import genai
from google.genai import errors
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash")
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]
CALENDAR_ID = os.environ["CALENDAR_ID"]
TIMEZONE = os.environ.get("TIMEZONE", "America/Boa_Vista")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "data/agenda-bot.db")
REMINDER_MINUTES = int(os.environ.get("REMINDER_MINUTES", "30"))
REMINDER_CHECK_SECONDS = int(os.environ.get("REMINDER_CHECK_SECONDS", "60"))
MATHEUS_REGISTRATION_CODE = os.environ.get("MATHEUS_REGISTRATION_CODE", "")
LETICIA_REGISTRATION_CODE = os.environ.get("LETICIA_REGISTRATION_CODE", "")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "WARNING").upper()
TZ = ZoneInfo(TIMEZONE)
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

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

EVENTO_SCHEMA = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string"},
        "data": {"type": "string", "format": "date"},
        "hora_inicio": {"type": "string", "description": "Horário no formato HH:MM"},
        "hora_fim": {
            "type": ["string", "null"],
            "description": "Horário no formato HH:MM ou null",
        },
        "responsavel": {"type": "string", "enum": ["Matheus", "Letícia", "Ambos"]},
    },
    "required": ["titulo", "data", "hora_inicio", "hora_fim", "responsavel"],
    "additionalProperties": False,
}

credentials = service_account.Credentials.from_service_account_file(
    GOOGLE_SERVICE_ACCOUNT_FILE,
    scopes=["https://www.googleapis.com/auth/calendar"],
)
calendar_service = build("calendar", "v3", credentials=credentials)


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


def gerar_resposta_gemini(prompt: str):
    modelos = list(dict.fromkeys(filter(None, [GEMINI_MODEL, GEMINI_FALLBACK_MODEL])))
    config = {
        "response_mime_type": "application/json",
        "response_json_schema": EVENTO_SCHEMA,
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


def extrair_evento(texto: str) -> dict:
    agora = datetime.now(TZ)
    prompt = f"""Você extrai dados de eventos de agenda a partir de mensagens em português do Brasil.
Data e hora atuais: {agora.strftime('%Y-%m-%d %H:%M')} ({TIMEZONE}), dia da semana: {agora.strftime('%A')}.

Regras:
- Resolva datas relativas ("hoje", "amanhã", nomes de dias da semana) com base na data atual acima.
- Se não der pra saber a hora de término, use null.
- Se não especificar responsável, use "Ambos".

Mensagem: "{texto}"
"""
    resposta = gerar_resposta_gemini(prompt)
    dados = json.loads(resposta.text)

    inicio = datetime.strptime(f"{dados['data']} {dados['hora_inicio']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    if dados.get("hora_fim"):
        fim = datetime.strptime(f"{dados['data']} {dados['hora_fim']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    else:
        fim = inicio + timedelta(minutes=30)

    return {
        "titulo": dados["titulo"],
        "responsavel": dados.get("responsavel", "Ambos"),
        "inicio": inicio,
        "fim": fim,
    }


def criar_evento_calendar(evento: dict) -> None:
    body = {
        "summary": evento["titulo"],
        "description": f"Responsável: {evento['responsavel']}",
        "start": {"dateTime": evento["inicio"].isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": evento["fim"].isoformat(), "timeZone": TIMEZONE},
        "colorId": COLOR_BY_RESPONSAVEL.get(evento["responsavel"], "2"),
    }
    calendar_service.events().insert(calendarId=CALENDAR_ID, body=body).execute()


def texto_normalizado(texto: str) -> str:
    return "".join(
        caractere
        for caractere in normalize("NFD", texto.casefold())
        if not combining(caractere)
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
    return hoje, hoje + timedelta(days=1), "hoje"


def buscar_eventos(inicio: datetime, fim: datetime) -> list[dict]:
    resposta = (
        calendar_service.events()
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


def formatar_agenda(eventos: list[dict], rotulo: str) -> str:
    if not eventos:
        return f"📅 Você não tem compromissos para {rotulo}."

    linhas = [f"📅 Compromissos para {rotulo}:"]
    for evento in eventos[:30]:
        titulo = evento.get("summary", "Sem título")
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
    update: Update, intervalo: tuple[datetime, datetime, str]
) -> None:
    inicio, fim, rotulo = intervalo
    eventos = buscar_eventos(inicio, fim)
    await update.message.reply_text(formatar_agenda(eventos, rotulo))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = role_do_usuario(update.effective_user.id)
    if role:
        mensagem = (
            f"Olá, {role}! Mande uma tarefa para criar um evento ou pergunte:\n"
            '"O que tenho hoje?"\n"Quais são meus compromissos de amanhã?"'
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
    if not role_do_usuario(update.effective_user.id):
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    texto = " ".join(context.args) if context.args else "o que tenho hoje"
    intervalo = intervalo_de_consulta(f"mostrar agenda {texto}")
    try:
        await responder_consulta(update, intervalo)
    except Exception as erro:
        LOGGER.exception("Falha ao consultar a agenda")
        await update.message.reply_text(f"❌ Não consegui consultar a agenda: {erro}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    texto = update.message.text
    if not role_do_usuario(update.effective_user.id):
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return

    try:
        intervalo = intervalo_de_consulta(texto)
        if intervalo:
            await responder_consulta(update, intervalo)
            return

        evento = extrair_evento(texto)
        criar_evento_calendar(evento)
        await update.message.reply_text(
            f"✅ {evento['titulo']} — {evento['inicio'].strftime('%d/%m %H:%M')} "
            f"({evento['responsavel']})"
        )
    except Exception as erro:
        LOGGER.exception("Falha ao processar mensagem")
        await update.message.reply_text(f"❌ Não consegui processar a mensagem: {erro}")


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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
