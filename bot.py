import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import google.generativeai as genai
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]
CALENDAR_ID = os.environ["CALENDAR_ID"]
TIMEZONE = os.environ.get("TIMEZONE", "America/Boa_Vista")
TZ = ZoneInfo(TIMEZONE)

# Mesma legenda de cor usada no colorizeRotina.gs (Documents/agenda/scripts)
COLOR_BY_RESPONSAVEL = {
    "Matheus": "9",   # Blueberry / Azul
    "Letícia": "4",   # Flamingo / Rosa
    "Ambos": "2",     # Sage / Verde
}

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-2.0-flash")

credentials = service_account.Credentials.from_service_account_file(
    GOOGLE_SERVICE_ACCOUNT_FILE,
    scopes=["https://www.googleapis.com/auth/calendar"],
)
calendar_service = build("calendar", "v3", credentials=credentials)


def extrair_evento(texto: str) -> dict:
    agora = datetime.now(TZ)
    prompt = f"""Você extrai dados de eventos de agenda a partir de mensagens em português do Brasil.
Data e hora atuais: {agora.strftime('%Y-%m-%d %H:%M')} ({TIMEZONE}), dia da semana: {agora.strftime('%A')}.

Responda SOMENTE com um JSON válido (sem markdown, sem texto extra), no formato exato:
{{"titulo": "...", "data": "YYYY-MM-DD", "hora_inicio": "HH:MM", "hora_fim": "HH:MM ou null", "responsavel": "Matheus" ou "Letícia" ou "Ambos"}}

Regras:
- Resolva datas relativas ("hoje", "amanhã", nomes de dias da semana) com base na data atual acima.
- Se não der pra saber a hora de término, use null.
- Se não especificar responsável, use "Ambos".

Mensagem: "{texto}"
"""
    resposta = gemini_model.generate_content(prompt).text.strip()
    resposta = re.sub(r"^```(json)?|```$", "", resposta, flags=re.MULTILINE).strip()
    dados = json.loads(resposta)

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Manda uma tarefa em texto livre, tipo:\n"
        "\"lembrar de pagar a conta de luz sexta às 18h\"\n"
        "\"amanhã às 9h dentista, é a Letícia\""
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    texto = update.message.text
    try:
        evento = extrair_evento(texto)
        criar_evento_calendar(evento)
        await update.message.reply_text(
            f"✅ {evento['titulo']} — {evento['inicio'].strftime('%d/%m %H:%M')} "
            f"({evento['responsavel']})"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Não consegui criar o evento: {e}")


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
