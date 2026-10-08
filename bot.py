import asyncio
import hmac
import json
import logging
import re
import secrets
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import errors, types
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

from agenda_bot.calendar_gateway import (
    create_event as _create_event,
    create_events_with_ids as _create_events_with_ids,
    delete_event as _delete_event,
    event_exists as _event_exists,
    fetch_events as _fetch_events,
    fetch_events_to_cancel as _fetch_events_to_cancel,
    restoration_body as _restoration_body,
    undo_action as _undo_action,
    update_event as _update_event,
)
from agenda_bot.schemas import (
    CANCELAMENTO_SCHEMA,
    EDICAO_SCHEMA,
    EVENTO_SCHEMA,
    INTENCAO_SCHEMA,
    REPLICACAO_SCHEMA,
    TAREFA_SCHEMA,
)
from agenda_bot.google_tasks import GoogleTasksGateway
from agenda_bot.handlers import preferences as preference_handlers
from agenda_bot.handlers import tasks as task_handlers
from agenda_bot import reminder_jobs
from agenda_bot.event_rules import (
    calculate_free_times as _calculate_free_times,
    conflicts_between_events as _conflicts_between_events,
    event_end as _event_end,
    event_responsible as _event_responsible,
    event_start as _event_start,
    event_summary as _event_summary,
    filter_events_by_period as _filter_events_by_period,
    filter_events_by_term as _filter_events_by_term,
    format_free_times as _format_free_times,
    format_schedule as _format_schedule,
    planned_event_description as _planned_event_description,
    prepare_edited_event as _prepare_edited_event,
    prepare_replicated_events as _prepare_replicated_events,
    replication_conflicts as _replication_conflicts,
    select_events_to_replicate as _select_events_to_replicate,
    task_due_label as _task_due_label,
)
from agenda_bot.intent_rules import (
    consulta_eh_pessoal,
    eh_consulta_agenda,
    eh_consulta_de_compromisso_especifico,
    eh_pedido_cancelamento,
    eh_pedido_criacao_evento,
    eh_pedido_criacao_tarefa,
    eh_pedido_edicao,
    eh_pedido_replicacao,
    intervalo_de_consulta,
    pedido_horarios_livres,
    pedido_planejamento_diario,
    periodo_do_texto,
    resolver_data_curta,
    texto_normalizado,
)
from agenda_bot.planning import (
    allocate_task_blocks,
    planned_task_ids,
    prioritize_tasks,
)
from agenda_bot.reminders import (
    REMINDER_DEFAULTS,
    REMINDER_SETTING_KEYS,
    reminder_heading,
    reminder_kind,
)
from agenda_bot.storage import AgendaRepository
from agenda_bot.settings import (
    APP_VERSION,
    BUILD_COMMIT,
    CALENDAR_ID,
    COLOR_BY_RESPONSAVEL,
    DATABASE_PATH,
    DEFAULT_RECURRENCE_DAYS,
    GEMINI_API_KEY,
    GEMINI_FALLBACK_MODEL,
    GEMINI_MODEL,
    GEMINI_TIMEOUT_SECONDS,
    GOOGLE_SERVICE_ACCOUNT_FILE,
    GOOGLE_TASKS_TOKEN_DIR,
    LLM_FALLBACK_TO_GEMINI,
    LOG_LEVEL,
    MAX_EVENT_RANGE_DAYS,
    MAX_RECURRENCE_OCCURRENCES,
    OLLAMA_BASE_URL,
    OLLAMA_ENABLED,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_MODEL,
    OLLAMA_NUM_CTX,
    OLLAMA_TIMEOUT_SECONDS,
    REGISTRATION_CODES,
    REMINDER_CHECK_SECONDS,
    REMINDER_MINUTES,
    STARTED_AT,
    TASKS_SCOPES,
    TELEGRAM_BOT_TOKEN,
    TIMEZONE,
    TZ,
)

LAST_REMINDER_CHECK: Optional[datetime] = None
LAST_REMINDER_ERROR: Optional[str] = None
LOGGER = logging.getLogger(__name__)

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

calendar_service = None


def _runtime_module():
    """Expõe as fachadas deste módulo aos handlers sem criar imports circulares."""
    return sys.modules[__name__]


def set_reminder_status(check_time: datetime, error: Optional[str]) -> None:
    global LAST_REMINDER_CHECK, LAST_REMINDER_ERROR
    LAST_REMINDER_CHECK = check_time
    LAST_REMINDER_ERROR = error


def obter_calendar_service():
    global calendar_service
    if calendar_service is None:
        credentials = service_account.Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE,
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
        calendar_service = build("calendar", "v3", credentials=credentials)
    return calendar_service


def _repository() -> AgendaRepository:
    # Criado sob demanda para que testes e scripts possam trocar DATABASE_PATH.
    return AgendaRepository(DATABASE_PATH, TZ, REMINDER_MINUTES)


def conectar_banco():
    return _repository().connect()


def inicializar_banco() -> None:
    _repository().initialize()


def listar_preferencias(role: str) -> list[sqlite3.Row]:
    return _repository().list_preferences(role)


def adicionar_preferencia(role: str, texto: str) -> None:
    _repository().add_preference(role, texto)


def remover_preferencia(role: str, indice: int) -> bool:
    return _repository().remove_preference(role, indice)


def obter_configuracao(role: str, chave: str, padrao: str) -> str:
    return _repository().get_setting(role, chave, padrao)


def salvar_configuracao(role: str, chave: str, valor: str) -> None:
    _repository().save_setting(role, chave, valor)


def salvar_rotina(role: str, nome: str, eventos: list[dict]) -> None:
    _repository().save_routine(role, nome, eventos)


def obter_rotina(role: str, nome: str) -> Optional[list[dict]]:
    return _repository().get_routine(role, nome)


def listar_rotinas(role: str) -> list[str]:
    return _repository().list_routines(role)


def registrar_acao(role: str, tipo: str, payload: dict) -> None:
    _repository().record_action(role, tipo, payload)


def ultima_acao_pendente(role: str) -> Optional[sqlite3.Row]:
    return _repository().last_pending_action(role)


def marcar_acao_desfeita(acao_id: int) -> None:
    _repository().mark_action_undone(acao_id)


def role_do_usuario(telegram_user_id: int) -> Optional[str]:
    return _repository().user_role(telegram_user_id)


def salvar_registro(role: str, telegram_user_id: int, chat_id: int) -> tuple[bool, str]:
    return _repository().save_registration(role, telegram_user_id, chat_id)


def destinatarios_do_evento(responsavel: str) -> list[sqlite3.Row]:
    return _repository().event_recipients(responsavel)


def notificacao_enviada(event_id: str, inicio: str, chat_id: int) -> bool:
    return _repository().notification_sent(event_id, inicio, chat_id)


def marcar_notificacao_enviada(
    event_id: str, inicio: str, chat_id: int, antecedencia: Optional[int] = None
) -> None:
    _repository().mark_notification_sent(
        event_id, inicio, chat_id, antecedencia
    )


def limpar_notificacoes_antigas() -> None:
    _repository().clean_old_notifications()


def obter_historico_conversa(role: str) -> list[dict]:
    return _repository().conversation_history(role)


def salvar_turno_conversa(role: str, usuario: str, assistente: str) -> None:
    _repository().save_conversation_turn(role, usuario, assistente)


def obter_estado_conversa(role: str) -> dict:
    return _repository().conversation_state(role)


def salvar_estado_conversa(role: str, estado: dict) -> None:
    _repository().save_conversation_state(role, estado)


def apagar_conversa(role: str) -> None:
    _repository().clear_conversation(role)


def resumo_proativo_enviado(role: str, dia: date, tipo: str) -> bool:
    return _repository().proactive_summary_sent(role, dia, tipo)


def marcar_resumo_proativo_enviado(role: str, dia: date, tipo: str) -> None:
    _repository().mark_proactive_summary_sent(role, dia, tipo)


def limpar_resumos_proativos_antigos() -> None:
    _repository().clean_old_proactive_summaries()


def configuracao_lembretes(
    role: str, valores: Optional[dict[str, str]] = None
) -> dict:
    def obter(chave: str, padrao: str) -> str:
        if valores is not None:
            return valores.get(chave, padrao)
        return obter_configuracao(role, chave, padrao)

    ativo = obter("lembretes_ativos", "sim").casefold() in {
        "1",
        "ligado",
        "on",
        "sim",
        "true",
    }
    configuracao = {"ativo": ativo}
    for tipo, padrao in REMINDER_DEFAULTS.items():
        chave = REMINDER_SETTING_KEYS[tipo]
        try:
            minutos = int(obter(chave, str(padrao)))
        except ValueError:
            minutos = padrao
        configuracao[tipo] = min(180, max(5, minutos))
    return configuracao


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


def extrair_cancelamento(
    texto: str, referencia: Optional[dict] = None
) -> tuple[Optional[str], datetime, datetime]:
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
    if referencia:
        dados["termo"] = referencia.get("titulo")
        if referencia.get("inicio"):
            dados["data"] = referencia["inicio"][:10]
            dados["data_fim"] = None

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
    texto: str, referencia: Optional[dict] = None
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
    if referencia:
        dados["termo"] = referencia.get("titulo")
        if referencia.get("inicio"):
            dados["data"] = referencia["inicio"][:10]

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
    return _prepare_edited_event(evento, alteracoes, TZ)


def descricao_evento_planejado(evento: dict) -> str:
    return _planned_event_description(evento)


def atualizar_evento_calendar(evento: dict) -> None:
    _update_event(
        evento,
        obter_calendar_service,
        CALENDAR_ID,
        TIMEZONE,
        COLOR_BY_RESPONSAVEL,
    )


def caminho_token_tasks(role: str) -> Path:
    return GoogleTasksGateway(GOOGLE_TASKS_TOKEN_DIR, TZ, TASKS_SCOPES).token_path(role)


def tasks_service_do_usuario(role: str):
    return GoogleTasksGateway(GOOGLE_TASKS_TOKEN_DIR, TZ, TASKS_SCOPES).service_for(role)


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


def extrair_replicacao(texto: str) -> tuple[date, date, list[str]]:
    agora = datetime.now(TZ)
    prompt = f"""Extraia um pedido para copiar eventos existentes entre dois dias.
Data e hora atuais: {agora.strftime('%Y-%m-%d %H:%M')} ({TIMEZONE}).

Regras:
- data_origem é o dia no qual os eventos já existem.
- data_destino é o novo dia que receberá as cópias.
- Resolva hoje, amanhã e dias da semana com base na data atual.
- Em "copie academia de terça para quarta", terça é a origem e quarta o destino.
- termos contém somente os nomes dos eventos citados, sem verbos ou datas.
- Separe nomes coordenados: "acordar, culto, café e trabalho" gera quatro termos.
- Para "copie minha rotina inteira", use termos=[].
- Se origem ou destino não estiver informado, use null; não invente a data.

Mensagem: {json.dumps(texto, ensure_ascii=False)}
"""
    dados = json.loads(gerar_json_operacao(prompt, REPLICACAO_SCHEMA))
    if not dados.get("data_origem") or not dados.get("data_destino"):
        raise ValueError(
            "informe o dia de origem e o destino, por exemplo: "
            "replica academia de terça para quarta"
        )
    data_origem = date.fromisoformat(dados["data_origem"])
    data_destino = date.fromisoformat(dados["data_destino"])
    if data_origem == data_destino:
        raise ValueError("o dia de origem e o destino precisam ser diferentes")
    termos = [
        termo.strip()
        for termo in dados.get("termos", [])
        if isinstance(termo, str) and termo.strip()
    ]
    return data_origem, data_destino, termos


def criar_tarefa_google(role: str, tarefa: dict) -> dict:
    return GoogleTasksGateway(GOOGLE_TASKS_TOKEN_DIR, TZ, TASKS_SCOPES).create(
        role, tarefa
    )


def listar_tarefas_google(role: str) -> list[dict]:
    return GoogleTasksGateway(
        GOOGLE_TASKS_TOKEN_DIR, TZ, TASKS_SCOPES
    ).list_pending(role)


def concluir_tarefa_google(role: str, task_id: str) -> None:
    GoogleTasksGateway(GOOGLE_TASKS_TOKEN_DIR, TZ, TASKS_SCOPES).complete(role, task_id)


def editar_tarefa_google(role: str, task_id: str, titulo: str) -> None:
    GoogleTasksGateway(GOOGLE_TASKS_TOKEN_DIR, TZ, TASKS_SCOPES).edit(
        role, task_id, titulo
    )


def excluir_tarefa_google(role: str, task_id: str) -> None:
    GoogleTasksGateway(GOOGLE_TASKS_TOKEN_DIR, TZ, TASKS_SCOPES).delete(role, task_id)


def evento_ja_existe(evento: dict) -> bool:
    return _event_exists(evento, obter_calendar_service, CALENDAR_ID, TZ)


def criar_evento_calendar(evento: dict) -> bool:
    return _create_event(
        evento,
        obter_calendar_service,
        CALENDAR_ID,
        TIMEZONE,
        TZ,
        COLOR_BY_RESPONSAVEL,
    )


def criar_eventos_calendar_com_ids(eventos: list[dict]) -> list[str]:
    return _create_events_with_ids(
        eventos,
        obter_calendar_service,
        CALENDAR_ID,
        TIMEZONE,
        TZ,
        COLOR_BY_RESPONSAVEL,
    )


def criar_eventos_calendar(eventos: list[dict]) -> int:
    return sum(criar_evento_calendar(evento) for evento in eventos)


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
- replicar_eventos: copiar eventos que já existem em um dia para outro dia.
- sugerir_horarios: encontrar horários livres para encaixar um compromisso.
- conversa: saudação, pergunta geral, conselho ou conversa sem operação solicitada.

Regras:
- Não transforme conversa casual em evento.
- Só use criar_evento/criar_tarefa quando houver um pedido explícito para adicionar.
- Uma afirmação como "vou ter uma reunião quinta" ou um pedido de conselho sobre onde
  encaixar algo é conversa, não criação.
- Só use editar_evento ou cancelar_evento quando a alteração/exclusão for explícita.
- Use replicar_eventos para pedidos explícitos com verbos como copiar, replicar,
  duplicar ou repetir eventos existentes em outro dia.
- Para sugerir_horarios, resolva a data e extraia a duração em minutos. Se a duração
  não for informada, use 60.
- Para consulta de agenda, resolva o período e preencha data/data_fim. data_fim é inclusiva.
- Se a pessoa perguntar por um compromisso específico, como "que horas é a academia?",
  preencha termo somente com o nome relevante, como "academia". Em consultas gerais,
  como "o que tenho hoje?", use termo=null.
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


def resolver_continuacao_agenda(texto: str, role: str) -> Optional[dict]:
    estado = obter_estado_conversa(role)
    if estado.get("tipo") != "consulta_agenda":
        return None

    normalizado = texto_normalizado(texto).strip()
    palavras = re.findall(r"[a-z0-9]+", normalizado)
    periodo_explicito = periodo_do_texto(texto)
    termo_data = re.search(
        r"\b(hoje|hj|amanha|segunda|terca|quarta|quinta|sexta|sabado|domingo|"
        r"\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b",
        normalizado,
    )
    tem_semana = "semana" in palavras
    palavras_de_consulta_completa = {
        "agenda",
        "compromisso",
        "compromissos",
        "evento",
        "eventos",
        "meu",
        "minha",
        "nosso",
        "nossa",
        "tenho",
        "temos",
    }
    parece_continuacao = normalizado.startswith(
        ("e ", "agora ", "entao ", "mas ")
    ) or (
        len(palavras) <= 3
        and not set(palavras) & palavras_de_consulta_completa
    )
    if not parece_continuacao or not (periodo_explicito or termo_data or tem_semana):
        return None

    if tem_semana:
        inicio = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        fim = inicio + timedelta(days=7 - inicio.weekday())
        rotulo = "esta semana"
    elif termo_data:
        data_consulta = resolver_data_curta(termo_data.group(1))
        inicio = datetime.combine(data_consulta, datetime.min.time(), tzinfo=TZ)
        fim = inicio + timedelta(days=1)
        hoje = datetime.now(TZ).date()
        if data_consulta == hoje:
            rotulo = "hoje"
        elif data_consulta == hoje + timedelta(days=1):
            rotulo = "amanhã"
        else:
            rotulo = data_consulta.strftime("%d/%m/%Y")
    else:
        try:
            inicio = datetime.fromisoformat(estado["inicio"]).astimezone(TZ)
            fim = datetime.fromisoformat(estado["fim"]).astimezone(TZ)
        except (KeyError, TypeError, ValueError):
            return None
        rotulo = estado.get("rotulo", inicio.strftime("%d/%m/%Y"))

    return {
        "intervalo": (inicio, fim, rotulo),
        "pessoal": bool(estado.get("pessoal")),
        "termo": estado.get("termo"),
        "periodo": periodo_explicito or estado.get("periodo"),
    }


def evento_referenciado(texto: str, role: str) -> Optional[dict]:
    palavras = set(re.findall(r"[a-z0-9]+", texto_normalizado(texto)))
    if not palavras & {"esse", "essa", "isso", "ele", "ela"}:
        return None
    return obter_estado_conversa(role).get("ultimo_evento")


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


def buscar_eventos(inicio: datetime, fim: datetime) -> list[dict]:
    return _fetch_events(
        inicio, fim, obter_calendar_service, CALENDAR_ID
    )


def buscar_eventos_para_cancelar(
    termo: Optional[str], inicio: datetime, fim: datetime
) -> list[dict]:
    return _fetch_events_to_cancel(
        termo, inicio, fim, obter_calendar_service, CALENDAR_ID
    )


def excluir_evento_calendar(event_id: str) -> None:
    _delete_event(event_id, obter_calendar_service, CALENDAR_ID)


def corpo_restauracao_evento(evento: dict) -> dict:
    return _restoration_body(evento)


def executar_desfazer(acao: sqlite3.Row) -> str:
    return _undo_action(acao, obter_calendar_service, CALENDAR_ID)


def inicio_do_evento(evento: dict) -> Optional[datetime]:
    return _event_start(evento, TZ)


def fim_do_evento(evento: dict) -> Optional[datetime]:
    return _event_end(evento, TZ)


def responsavel_do_evento(evento: dict) -> str:
    return _event_responsible(evento)


def resumo_evento(evento: dict) -> str:
    return _event_summary(evento, TZ)


def formatar_agenda(eventos: list[dict], rotulo: str) -> str:
    return _format_schedule(eventos, rotulo, TZ)


def filtrar_eventos_por_periodo(eventos: list[dict], periodo: str) -> list[dict]:
    return _filter_events_by_period(eventos, periodo, TZ)


def filtrar_eventos_por_termo(eventos: list[dict], termo: str) -> list[dict]:
    return _filter_events_by_term(eventos, termo)


def selecionar_eventos_para_replicar(
    eventos: list[dict], termos: list[str]
) -> list[dict]:
    return _select_events_to_replicate(eventos, termos, TZ)


def preparar_eventos_replicados(
    eventos: list[dict], data_destino: date
) -> list[dict]:
    return _prepare_replicated_events(eventos, data_destino, TZ)


def conflitos_da_replicacao(
    eventos_novos: list[dict], ignorar_ids: Optional[set[str]] = None
) -> list[str]:
    return _replication_conflicts(
        eventos_novos, TZ, buscar_eventos, ignorar_ids
    )


async def responder_consulta(
    update: Update,
    intervalo: tuple[datetime, datetime, str],
    role: Optional[str] = None,
    termo: Optional[str] = None,
    periodo: Optional[str] = None,
    actor_role: Optional[str] = None,
    texto_usuario: Optional[str] = None,
) -> None:
    inicio, fim, rotulo = intervalo
    rotulo_base = rotulo
    eventos = await asyncio.to_thread(buscar_eventos, inicio, fim)
    if role:
        eventos = [
            evento
            for evento in eventos
            if responsavel_do_evento(evento) in {role, "Ambos"}
        ]
    if periodo:
        eventos = filtrar_eventos_por_periodo(eventos, periodo)
        rotulo = f"{rotulo} " + {
            "madrugada": "de madrugada",
            "manhã": "pela manhã",
            "tarde": "à tarde",
            "noite": "à noite",
        }[periodo]
    if termo:
        eventos = filtrar_eventos_por_termo(eventos, termo)
        if not eventos:
            resposta = f'🔎 Não encontrei "{termo}" na agenda para {rotulo}.'
            if actor_role:
                await asyncio.to_thread(
                    registrar_contexto_consulta,
                    actor_role,
                    inicio,
                    fim,
                    rotulo_base,
                    role is not None,
                    termo,
                    periodo,
                    eventos,
                    texto_usuario,
                    resposta,
                )
            await update.message.reply_text(resposta)
            return
    resposta = formatar_agenda(eventos, rotulo)
    if actor_role:
        await asyncio.to_thread(
            registrar_contexto_consulta,
            actor_role,
            inicio,
            fim,
            rotulo_base,
            role is not None,
            termo,
            periodo,
            eventos,
            texto_usuario,
            resposta,
        )
    await update.message.reply_text(resposta)


def registrar_contexto_consulta(
    role: str,
    inicio: datetime,
    fim: datetime,
    rotulo: str,
    pessoal: bool,
    termo: Optional[str],
    periodo: Optional[str],
    eventos: list[dict],
    texto_usuario: Optional[str],
    resposta: str,
) -> None:
    estado = {
        "tipo": "consulta_agenda",
        "inicio": inicio.isoformat(),
        "fim": fim.isoformat(),
        "rotulo": rotulo,
        "pessoal": pessoal,
        "termo": termo,
        "periodo": periodo,
    }
    if len(eventos) == 1:
        evento = eventos[0]
        estado["ultimo_evento"] = {
            "id": evento.get("id"),
            "titulo": evento.get("summary", "Sem título"),
            "inicio": evento.get("start", {}).get("dateTime")
            or evento.get("start", {}).get("date"),
            "responsavel": responsavel_do_evento(evento),
        }
    salvar_estado_conversa(role, estado)
    if texto_usuario:
        salvar_turno_conversa(role, texto_usuario, resposta)


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


def calcular_horarios_livres(
    role: str,
    data_consulta: date,
    duracao_minutos: int,
    eventos_existentes: Optional[list[dict]] = None,
) -> list[tuple[datetime, datetime]]:
    return _calculate_free_times(
        role,
        data_consulta,
        duracao_minutos,
        TZ,
        obter_configuracao,
        buscar_eventos,
        eventos_existentes,
    )


def formatar_horarios_livres(
    data_consulta: date,
    duracao_minutos: int,
    horarios: list[tuple[datetime, datetime]],
) -> str:
    return _format_free_times(data_consulta, duracao_minutos, horarios)


def conflitos_entre_eventos(eventos: list[dict]) -> list[str]:
    return _conflicts_between_events(eventos, TZ)


def rotulo_prazo_tarefa(tarefa: dict, data_consulta: date) -> str:
    return _task_due_label(tarefa, data_consulta)


def carregar_tarefas_planejamento(role: str) -> tuple[list[dict], Optional[str]]:
    try:
        return listar_tarefas_google(role), None
    except Exception as erro:
        LOGGER.warning("Google Tarefas indisponível no planejamento de %s: %s", role, erro)
        return [], "Google Tarefas indisponível; o plano considera somente a agenda."


def montar_planejamento_diario(role: str, data_consulta: date) -> dict:
    inicio = datetime.combine(data_consulta, datetime.min.time(), tzinfo=TZ)
    fim = inicio + timedelta(days=1)
    todos_eventos = buscar_eventos(inicio, fim)
    eventos = [
        evento
        for evento in todos_eventos
        if responsavel_do_evento(evento) in {role, "Ambos"}
    ]
    tarefas, aviso_tarefas = carregar_tarefas_planejamento(role)
    try:
        duracao = int(obter_configuracao(role, "duracao_tarefa", "30"))
    except ValueError:
        duracao = 30
    duracao = min(120, max(15, duracao))
    prioritarias = prioritize_tasks(
        tarefas,
        data_consulta,
        ignored_ids=planned_task_ids(eventos),
        limit=5,
    )
    horarios = calcular_horarios_livres(
        role, data_consulta, duracao, todos_eventos
    )
    blocos, nao_alocadas = allocate_task_blocks(
        prioritarias, horarios, duracao, role
    )
    conflitos = conflitos_entre_eventos(eventos)

    linhas = [f"🧭 Plano de {data_consulta.strftime('%d/%m/%Y')}"]
    if eventos:
        linhas.append("\n📅 Compromissos fixos:")
        for evento in eventos[:12]:
            inicio_evento = inicio_do_evento(evento)
            fim_evento = fim_do_evento(evento)
            horario = (
                f"{inicio_evento.strftime('%H:%M')}–{fim_evento.strftime('%H:%M')}"
                if inicio_evento and fim_evento
                else "dia inteiro"
            )
            linhas.append(f"\n• {horario} — {evento.get('summary', 'Sem título')}")
    else:
        linhas.append("\n📅 Nenhum compromisso fixo.")

    if blocos:
        linhas.append(f"\n\n✅ Sugestão para tarefas ({duracao} min cada):")
        for bloco in blocos:
            linhas.append(
                f"\n• {bloco['inicio'].strftime('%H:%M')}–"
                f"{bloco['fim'].strftime('%H:%M')} — {bloco['titulo'][6:]}"
            )
    elif prioritarias:
        linhas.append("\n\n⏱ Não encontrei espaço para as tarefas prioritárias.")
    else:
        linhas.append("\n\n✅ Nenhuma tarefa vencida ou sem prazo para encaixar.")

    if nao_alocadas:
        linhas.append("\n\n⚠️ Não couberam no dia:")
        for tarefa in nao_alocadas:
            linhas.append(
                f"\n• {tarefa.get('title', 'Sem título')} "
                f"({rotulo_prazo_tarefa(tarefa, data_consulta)})"
            )
    if conflitos:
        linhas.append("\n\n⚠️ Conflitos existentes:")
        linhas.extend(f"\n• {item}" for item in conflitos[:5])
    if aviso_tarefas:
        linhas.append(f"\n\nℹ️ {aviso_tarefas}")
    if blocos:
        linhas.append("\n\nNada será alterado sem sua confirmação.")
    return {"texto": "".join(linhas), "blocos": blocos}


def montar_resumo_proativo(
    role: str,
    tipo: str,
    agora: datetime,
    eventos_do_periodo: Optional[list[dict]] = None,
) -> Optional[str]:
    data_consulta = agora.date() if tipo == "manha" else agora.date() + timedelta(days=1)
    inicio = datetime.combine(data_consulta, datetime.min.time(), tzinfo=TZ)
    eventos_base = (
        eventos_do_periodo
        if eventos_do_periodo is not None
        else buscar_eventos(inicio, inicio + timedelta(days=1))
    )
    eventos = [
        evento
        for evento in eventos_base
        if responsavel_do_evento(evento) in {role, "Ambos"}
    ]
    tarefas, _ = carregar_tarefas_planejamento(role)
    tarefas = prioritize_tasks(
        tarefas,
        data_consulta,
        ignored_ids=planned_task_ids(eventos),
        limit=5,
    )
    conflitos = conflitos_entre_eventos(eventos)
    if not eventos and not tarefas and not conflitos:
        return None

    titulo = "☀️ Seu dia de hoje" if tipo == "manha" else "🌙 Prévia de amanhã"
    linhas = [titulo]
    if eventos:
        linhas.append("\n\n📅 Agenda:")
        for evento in eventos[:10]:
            inicio_evento = inicio_do_evento(evento)
            horario = inicio_evento.strftime("%H:%M") if inicio_evento else "dia inteiro"
            linhas.append(f"\n• {horario} — {evento.get('summary', 'Sem título')}")
    if tarefas:
        linhas.append("\n\n📋 Pendências prioritárias:")
        for tarefa in tarefas:
            linhas.append(
                f"\n• {tarefa.get('title', 'Sem título')} "
                f"({rotulo_prazo_tarefa(tarefa, data_consulta)})"
            )
    if conflitos:
        linhas.append("\n\n⚠️ Atenção:")
        linhas.extend(f"\n• {item}" for item in conflitos[:5])
    if tarefas:
        termo = "hoje" if tipo == "manha" else "amanhã"
        linhas.append(f"\n\nUse /planejar {termo} para montar os blocos do dia.")
    return "".join(linhas)


async def enviar_planejamento_diario(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    role: str,
    data_consulta: date,
) -> None:
    plano = await asyncio.to_thread(montar_planejamento_diario, role, data_consulta)
    blocos = plano["blocos"]
    if not blocos:
        await update.message.reply_text(plano["texto"])
        return
    token = salvar_acao_pendente(
        context, "criacoes_pendentes", {"eventos": blocos}
    )
    await update.message.reply_text(
        plano["texto"],
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Adicionar blocos à agenda", callback_data=f"gc:{token}"
                    )
                ],
                [InlineKeyboardButton("Só visualizar", callback_data=f"ga:{token}")],
            ]
        ),
    )


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
    if len({evento["titulo"] for evento in eventos}) == 1:
        return (
            f"{primeiro['titulo']}\n"
            f"🗓 {len(eventos)} eventos, de {primeiro['inicio'].strftime('%d/%m/%Y')} "
            f"a {eventos[-1]['inicio'].strftime('%d/%m/%Y')}\n"
            f"🕒 {primeiro['inicio'].strftime('%H:%M')}–{primeiro['fim'].strftime('%H:%M')}\n"
            f"👤 {primeiro['responsavel']}"
        )

    linhas = [f"{len(eventos)} eventos:"]
    for evento in eventos[:15]:
        linhas.append(
            f"\n• {evento['inicio'].strftime('%d/%m %H:%M')}–"
            f"{evento['fim'].strftime('%H:%M')} — {evento['titulo']} "
            f"({evento['responsavel']})"
        )
    if len(eventos) > 15:
        linhas.append(f"\n… e mais {len(eventos) - 15} eventos.")
    return "".join(linhas)


async def mostrar_confirmacao_criacao(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    eventos: list[dict],
    cabecalho: str = "Confira antes de criar:",
    avisos: Optional[list[str]] = None,
) -> None:
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
    texto_avisos = ""
    if avisos:
        itens = "\n".join(f"• {aviso}" for aviso in dict.fromkeys(avisos[:5]))
        texto_avisos = f"\n\n⚠️ Conflitos no destino:\n{itens}"
    await update.message.reply_text(
        f"{cabecalho}\n\n{resumo_criacao(eventos)}{texto_avisos}",
        reply_markup=teclado,
    )


async def iniciar_criacao(
    update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str
) -> None:
    eventos = await asyncio.to_thread(extrair_eventos, texto)
    conflitos = await asyncio.to_thread(conflitos_da_replicacao, eventos)
    await mostrar_confirmacao_criacao(update, context, eventos, avisos=conflitos)


async def iniciar_replicacao(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    role: str,
    texto: str,
) -> None:
    data_origem, data_destino, termos = await asyncio.to_thread(
        extrair_replicacao, texto
    )
    inicio_origem = datetime.combine(data_origem, datetime.min.time(), tzinfo=TZ)
    eventos_origem = await asyncio.to_thread(
        buscar_eventos, inicio_origem, inicio_origem + timedelta(days=1)
    )
    if not termos:
        eventos_origem = [
            evento
            for evento in eventos_origem
            if responsavel_do_evento(evento) in {role, "Ambos"}
        ]
    selecionados = selecionar_eventos_para_replicar(eventos_origem, termos)
    if not selecionados:
        raise ValueError("não encontrei eventos para replicar no dia de origem")
    if len(selecionados) > 30:
        raise ValueError("a replicação permite no máximo 30 eventos por vez")
    copias = preparar_eventos_replicados(selecionados, data_destino)
    conflitos = await asyncio.to_thread(conflitos_da_replicacao, copias)
    await mostrar_confirmacao_criacao(
        update,
        context,
        copias,
        cabecalho=(
            f"Confira a cópia de {data_origem.strftime('%d/%m/%Y')} "
            f"para {data_destino.strftime('%d/%m/%Y')}:"
        ),
        avisos=conflitos,
    )


async def callback_criacao(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    role = role_do_usuario(query.from_user.id)
    if not role:
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
        ids_criados = await asyncio.to_thread(criar_eventos_calendar_com_ids, eventos)
        quantidade_criada = len(ids_criados)
    except Exception:
        LOGGER.exception("Falha ao criar eventos confirmados")
        await query.edit_message_text(
            "❌ Não consegui criar o evento agora. Tente novamente em alguns minutos."
        )
        return
    if ids_criados:
        try:
            registrar_acao(role, "criacao", {"event_ids": ids_criados})
        except sqlite3.Error:
            LOGGER.exception("Evento criado, mas a auditoria não pôde ser registrada")

    pendentes.pop(token, None)
    quantidade_existente = len(eventos) - quantidade_criada
    mensagem = f"✅ {quantidade_criada} evento(s) criado(s)."
    if quantidade_existente:
        mensagem += f"\nℹ️ {quantidade_existente} já existia(m) e não foi(ram) duplicado(s)."
    await query.edit_message_text(f"{mensagem}\n\n{resumo_criacao(eventos)}")


def resumo_tarefa(tarefa: dict) -> str:
    return task_handlers.task_summary(tarefa)


async def iniciar_criacao_tarefa(
    update: Update, context: ContextTypes.DEFAULT_TYPE, role: str, texto: str
) -> None:
    await task_handlers.begin_task_creation(
        _runtime_module(), update, context, role, texto
    )


async def callback_tarefa(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await task_handlers.task_creation_callback(
        _runtime_module(), update, context
    )


def resumo_edicao(
    evento_original: dict, evento_novo: dict, avisos: Optional[list[str]] = None
) -> str:
    texto = (
        f"Antes:\n{resumo_evento(evento_original)}\n\n"
        f"Depois:\n{evento_novo['titulo']} — "
        f"{evento_novo['inicio'].strftime('%d/%m/%Y às %H:%M')}–"
        f"{evento_novo['fim'].strftime('%H:%M')} "
        f"({evento_novo['responsavel']})"
    )
    if avisos:
        texto += "\n\n⚠️ Conflitos:\n" + "\n".join(
            f"• {aviso}" for aviso in dict.fromkeys(avisos[:5])
        )
    return texto


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
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    texto: str,
    referencia: Optional[dict] = None,
) -> None:
    termo, inicio, fim, alteracoes = await asyncio.to_thread(
        extrair_edicao, texto, referencia
    )
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
        conflitos = await asyncio.to_thread(
            conflitos_da_replicacao, [evento_novo], {eventos[0]["id"]}
        )
        await update.message.reply_text(
            resumo_edicao(eventos[0], evento_novo, conflitos),
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
    role = role_do_usuario(query.from_user.id)
    if not role:
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
        conflitos = await asyncio.to_thread(
            conflitos_da_replicacao, [evento_novo], {evento_original["id"]}
        )
        await query.edit_message_text(
            resumo_edicao(evento_original, evento_novo, conflitos),
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
        try:
            registrar_acao(role, "edicao", {"evento": evento_original})
        except sqlite3.Error:
            LOGGER.exception("Evento editado, mas a auditoria não pôde ser registrada")
        pendentes.pop(token, None)
        await query.edit_message_text(
            f"✅ Evento alterado.\n\n{resumo_edicao(evento_original, evento_novo)}"
        )


async def iniciar_cancelamento(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    texto: str,
    referencia: Optional[dict] = None,
) -> None:
    termo, inicio, fim = await asyncio.to_thread(
        extrair_cancelamento, texto, referencia
    )
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
    role = role_do_usuario(query.from_user.id)
    if not role:
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
        try:
            registrar_acao(role, "cancelamento", {"evento": evento})
        except sqlite3.Error:
            LOGGER.exception("Evento cancelado, mas a auditoria não pôde ser registrada")
        pendentes.pop(token, None)
        await query.edit_message_text(f"✅ Evento cancelado:\n\n{resumo_evento(evento)}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = role_do_usuario(update.effective_user.id)
    if role:
        mensagem = (
            f"Olá, {role}! Posso conversar e cuidar da agenda e das tarefas.\n"
            '"O que tenho hoje?"\n"Quais são meus compromissos de amanhã?"\n'
            '"Muda a academia de amanhã para 18h30"\n'
            '"Replica academia de terça para quarta"\n'
            '"Cancelar academia de amanhã"\n'
            '"Onde cabe uma reunião de 1 hora amanhã?"\n\n'
            '"Organiza meu dia amanhã"\n\n'
            "Principais comandos:\n"
            "/agenda · /editar · /replicar · /cancelar\n"
            "/tarefa · /tarefas · /concluir · /editar_tarefa · /excluir_tarefa\n"
            "/livres · /planejar · /preferencias · /rotinas\n"
            "/lembretes · /resumo_diario\n"
            "/desfazer · /status · /versao"
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
            update,
            intervalo,
            role if consulta_eh_pessoal(texto) else None,
            periodo=periodo_do_texto(texto),
            actor_role=role,
            texto_usuario=f"/agenda {texto}",
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


async def replicar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    if not context.args:
        await update.message.reply_text(
            "Use: /replicar EVENTOS DE ORIGEM PARA DESTINO\n"
            "Exemplo: /replicar academia e trabalho de terça para quarta"
        )
        return
    try:
        await iniciar_replicacao(update, context, role, " ".join(context.args))
    except Exception as erro:
        LOGGER.exception("Falha ao preparar replicação")
        await update.message.reply_text(
            mensagem_erro_usuario("preparar a replicação", erro)
        )


async def tarefa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await task_handlers.create_task_command(_runtime_module(), update, context)


async def tarefas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await task_handlers.list_tasks_command(_runtime_module(), update, context)


async def concluir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await task_handlers.complete_task_command(_runtime_module(), update, context)


async def concluir_tarefa_da_lista(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    role: str,
    numero: int,
) -> None:
    await task_handlers.complete_listed_task(
        _runtime_module(), update, context, role, numero
    )


async def editar_tarefa_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await task_handlers.edit_task_command(_runtime_module(), update, context)


async def excluir_tarefa_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await task_handlers.delete_task_command(_runtime_module(), update, context)


async def callback_alteracao_tarefa(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await task_handlers.task_change_callback(
        _runtime_module(), update, context
    )


async def limpar_conversa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await preference_handlers.clear_conversation(_runtime_module(), update, context)


async def preferencia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await preference_handlers.save_preference(_runtime_module(), update, context)


async def preferencias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await preference_handlers.list_preferences(_runtime_module(), update, context)


async def remover_preferencia_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await preference_handlers.remove_preference(_runtime_module(), update, context)


async def livres(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await preference_handlers.free_times(_runtime_module(), update, context)


async def planejar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await preference_handlers.plan_day(_runtime_module(), update, context)


async def salvar_rotina_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await preference_handlers.save_routine(_runtime_module(), update, context)


async def aplicar_rotina_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await preference_handlers.apply_routine(_runtime_module(), update, context)


async def rotinas_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await preference_handlers.list_routines(_runtime_module(), update, context)


async def resumo_diario_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await preference_handlers.configure_daily_summary(
        _runtime_module(), update, context
    )


async def lembretes_cmd(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await preference_handlers.configure_reminders(_runtime_module(), update, context)


async def desfazer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    acao = ultima_acao_pendente(role)
    if not acao:
        await update.message.reply_text("Não há ação das últimas 24 horas para desfazer.")
        return
    nomes = {"criacao": "criação", "edicao": "edição", "cancelamento": "cancelamento"}
    await update.message.reply_text(
        f"Desfazer a última {nomes.get(acao['tipo'], acao['tipo'])}?",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Confirmar", callback_data=f"uc:{acao['id']}")],
             [InlineKeyboardButton("Não desfazer", callback_data=f"ua:{acao['id']}")]]
        ),
    )


async def callback_desfazer(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    role = role_do_usuario(query.from_user.id)
    if not role:
        await query.answer("Telegram não autorizado.", show_alert=True)
        return
    await query.answer()
    acao_codigo, id_texto = query.data.split(":", 1)
    if acao_codigo == "ua":
        await query.edit_message_text("👍 Nada foi desfeito.")
        return
    acao = ultima_acao_pendente(role)
    if not acao or acao["id"] != int(id_texto):
        await query.edit_message_text("Esta ação não está mais disponível.")
        return
    try:
        resultado = await asyncio.to_thread(executar_desfazer, acao)
        marcar_acao_desfeita(acao["id"])
    except Exception as erro:
        await query.edit_message_text(mensagem_erro_usuario("desfazer a ação", erro))
        return
    await query.edit_message_text(f"✅ Ação desfeita: {resultado}.")


async def responder_conversa(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    role: str,
    texto: str,
) -> None:
    historico = await asyncio.to_thread(obter_historico_conversa, role)
    agora = datetime.now(TZ)
    try:
        contexto_agenda = await asyncio.to_thread(contexto_agenda_para_conversa, role)
    except Exception:
        LOGGER.exception("Falha ao carregar contexto da agenda para conversa")
        contexto_agenda = "A agenda não pôde ser consultada neste momento."
    preferencias_salvas = [item["texto"] for item in listar_preferencias(role)]
    contexto_preferencias = (
        "Preferências permanentes do usuário:\n- " + "\n- ".join(preferencias_salvas)
        if preferencias_salvas
        else "Preferências permanentes do usuário: nenhuma salva."
    )
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
                f"{contexto_agenda}\n\n{contexto_preferencias}"
            ),
        },
        *historico[-4:],
        {"role": "user", "content": texto},
    ]
    resposta = await asyncio.to_thread(gerar_conversa_llm, mensagens)
    await asyncio.to_thread(
        salvar_turno_conversa,
        role,
        texto,
        resposta,
    )
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


def obter_status_servicos(role: str) -> list[str]:
    linhas = []
    try:
        obter_calendar_service().events().list(
            calendarId=CALENDAR_ID, maxResults=1
        ).execute()
        linhas.append("✅ Google Agenda")
    except Exception:
        linhas.append("❌ Google Agenda")
    linhas.append(
        "✅ Google Tarefas"
        if caminho_token_tasks(role).is_file()
        else "❌ Google Tarefas sem autorização"
    )
    if OLLAMA_ENABLED:
        try:
            resposta = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
            resposta.raise_for_status()
            modelos = [item.get("name", "") for item in resposta.json().get("models", [])]
            linhas.append(
                f"✅ Ollama ({OLLAMA_MODEL})"
                if any(modelo.startswith(OLLAMA_MODEL) for modelo in modelos)
                else f"⚠️ Ollama ativo, mas sem {OLLAMA_MODEL}"
            )
        except requests.RequestException:
            linhas.append("❌ Ollama")
    try:
        atual = int(Path("/sys/fs/cgroup/memory.current").read_text().strip())
        limite_texto = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if limite_texto != "max":
            limite = int(limite_texto)
            linhas.append(f"ℹ️ RAM do bot: {atual / 2**20:.0f}/{limite / 2**20:.0f} MB")
    except (OSError, ValueError):
        pass
    if LAST_REMINDER_CHECK:
        linhas.append(
            f"{'❌' if LAST_REMINDER_ERROR else '✅'} Lembretes verificados às "
            f"{LAST_REMINDER_CHECK.strftime('%H:%M:%S')}"
        )
        if LAST_REMINDER_ERROR:
            linhas.append(f"⚠️ Último erro: {LAST_REMINDER_ERROR[:100]}")
    else:
        linhas.append("⏳ Lembretes ainda não verificados desde a inicialização")
    return linhas


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    role = role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    linhas = await asyncio.to_thread(obter_status_servicos, role)
    await update.message.reply_text(
        f"🤖 Agenda Bot v{APP_VERSION} ({BUILD_COMMIT})\n" + "\n".join(linhas)
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    texto = update.message.text
    role = role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return

    try:
        normalizado = texto_normalizado(texto)
        if "tarefa" in normalizado:
            numero = re.search(
                r"\b(?:a\s+)?tarefa\s+(?:numero\s+)?(\d+)\b", normalizado
            )
            if numero and eh_pedido_cancelamento(texto):
                context.args = [numero.group(1)]
                await excluir_tarefa_cmd(update, context)
                return
            edicao_tarefa = re.search(
                r"\b(?:a\s+)?tarefa\s+(?:n[uú]mero\s+)?(\d+)"
                r".*?\bpara\s+(.+)$",
                texto,
                re.IGNORECASE,
            )
            if edicao_tarefa and eh_pedido_edicao(texto):
                context.args = [edicao_tarefa.group(1), edicao_tarefa.group(2)]
                await editar_tarefa_cmd(update, context)
                return
            if eh_pedido_cancelamento(texto) or eh_pedido_edicao(texto):
                await update.message.reply_text(
                    "Primeiro use /tarefas; depois informe o número, por exemplo: "
                    "/excluir_tarefa 2 ou /editar_tarefa 2 Novo título."
                )
                return

        data_planejamento = pedido_planejamento_diario(texto)
        if data_planejamento:
            await enviar_planejamento_diario(
                update, context, role, data_planejamento
            )
            return

        pedido_livre = pedido_horarios_livres(texto)
        if pedido_livre:
            data_consulta, duracao = pedido_livre
            horarios = await asyncio.to_thread(
                calcular_horarios_livres, role, data_consulta, duracao
            )
            await update.message.reply_text(
                formatar_horarios_livres(data_consulta, duracao, horarios)
            )
            return

        if eh_pedido_replicacao(texto):
            await iniciar_replicacao(update, context, role, texto)
            return

        if eh_pedido_cancelamento(texto):
            await iniciar_cancelamento(
                update, context, texto, evento_referenciado(texto, role)
            )
            return

        if eh_pedido_edicao(texto):
            await iniciar_edicao(
                update, context, texto, evento_referenciado(texto, role)
            )
            return

        continuacao = await asyncio.to_thread(
            resolver_continuacao_agenda, texto, role
        )
        if continuacao:
            await responder_consulta(
                update,
                continuacao["intervalo"],
                role if continuacao["pessoal"] else None,
                continuacao["termo"],
                continuacao["periodo"],
                role,
                texto,
            )
            return

        intervalo = (
            None
            if eh_consulta_de_compromisso_especifico(texto)
            else intervalo_de_consulta(texto)
        )
        if intervalo:
            await responder_consulta(
                update,
                intervalo,
                role if consulta_eh_pessoal(texto) else None,
                periodo=periodo_do_texto(texto),
                actor_role=role,
                texto_usuario=texto,
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
                dados.get("termo"),
                periodo_do_texto(texto),
                role,
                texto,
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
            await iniciar_cancelamento(
                update, context, texto, evento_referenciado(texto, role)
            )
        elif intencao == "editar_evento":
            await iniciar_edicao(
                update, context, texto, evento_referenciado(texto, role)
            )
        elif intencao == "replicar_eventos":
            await iniciar_replicacao(update, context, role, texto)
        elif intencao == "sugerir_horarios":
            intervalo = intervalo_da_intencao(dados)
            data_consulta = intervalo[0].date()
            duracao = dados.get("duracao_minutos") or 60
            horarios = await asyncio.to_thread(
                calcular_horarios_livres, role, data_consulta, duracao
            )
            await update.message.reply_text(
                formatar_horarios_livres(data_consulta, duracao, horarios)
            )
        else:
            await responder_conversa(update, context, role, texto)
    except Exception as erro:
        LOGGER.exception("Falha ao processar mensagem")
        await update.message.reply_text(
            mensagem_erro_usuario("processar a mensagem", erro)
        )


async def verificar_lembretes(context: ContextTypes.DEFAULT_TYPE) -> None:
    await reminder_jobs.check_event_reminders(_runtime_module(), context)


async def enviar_lembrete_adiado(context: ContextTypes.DEFAULT_TYPE) -> None:
    await reminder_jobs.send_snoozed_reminder(context)


async def callback_lembrete(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await reminder_jobs.reminder_callback(_runtime_module(), update, context)


async def verificar_resumos_diarios(context: ContextTypes.DEFAULT_TYPE) -> None:
    await reminder_jobs.check_daily_summaries(_runtime_module(), context)


async def configurar_jobs(application: Application) -> None:
    await reminder_jobs.configure_jobs(_runtime_module(), application)


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
    app.add_handler(CommandHandler("replicar", replicar))
    app.add_handler(CommandHandler("tarefa", tarefa))
    app.add_handler(CommandHandler("tarefas", tarefas))
    app.add_handler(CommandHandler("concluir", concluir))
    app.add_handler(CommandHandler("editar_tarefa", editar_tarefa_cmd))
    app.add_handler(CommandHandler("excluir_tarefa", excluir_tarefa_cmd))
    app.add_handler(CommandHandler("livres", livres))
    app.add_handler(CommandHandler("planejar", planejar_cmd))
    app.add_handler(CommandHandler("preferencia", preferencia))
    app.add_handler(CommandHandler("preferencias", preferencias))
    app.add_handler(CommandHandler("remover_preferencia", remover_preferencia_cmd))
    app.add_handler(CommandHandler("salvar_rotina", salvar_rotina_cmd))
    app.add_handler(CommandHandler("aplicar_rotina", aplicar_rotina_cmd))
    app.add_handler(CommandHandler("rotinas", rotinas_cmd))
    app.add_handler(CommandHandler("lembretes", lembretes_cmd))
    app.add_handler(CommandHandler("resumo_diario", resumo_diario_cmd))
    app.add_handler(CommandHandler("desfazer", desfazer_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("limpar_conversa", limpar_conversa))
    app.add_handler(CommandHandler("versao", versao))
    app.add_handler(CallbackQueryHandler(callback_criacao, pattern=r"^g[ca]:"))
    app.add_handler(CallbackQueryHandler(callback_tarefa, pattern=r"^t[ca]:"))
    app.add_handler(
        CallbackQueryHandler(callback_alteracao_tarefa, pattern=r"^t(?:uc|ua|dc|da):")
    )
    app.add_handler(CallbackQueryHandler(callback_desfazer, pattern=r"^u[ca]:"))
    app.add_handler(CallbackQueryHandler(callback_lembrete, pattern=r"^l[ra]:"))
    app.add_handler(CallbackQueryHandler(callback_edicao, pattern=r"^e[sca]:"))
    app.add_handler(
        CallbackQueryHandler(callback_cancelamento, pattern=r"^c[sca]:")
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
