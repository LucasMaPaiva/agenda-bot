"""Schemas de respostas estruturadas esperadas dos modelos de linguagem."""

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

REPLICACAO_SCHEMA = {
    "type": "object",
    "properties": {
        "data_origem": {
            "type": ["string", "null"],
            "format": "date",
            "description": "Dia do qual os eventos serão copiados",
        },
        "data_destino": {
            "type": ["string", "null"],
            "format": "date",
            "description": "Dia para o qual os eventos serão copiados",
        },
        "termos": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Nomes dos eventos pedidos; vazio significa toda a rotina",
        },
    },
    "required": ["data_origem", "data_destino", "termos"],
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
                "replicar_eventos",
                "sugerir_horarios",
                "conversa",
            ],
        },
        "data": {"type": ["string", "null"], "format": "date"},
        "data_fim": {"type": ["string", "null"], "format": "date"},
        "pessoal": {"type": "boolean"},
        "termo": {
            "type": ["string", "null"],
            "description": "Nome do compromisso específico consultado ou null",
        },
        "numero_tarefa": {"type": ["integer", "null"]},
        "duracao_minutos": {"type": ["integer", "null"]},
    },
    "required": [
        "intencao",
        "data",
        "data_fim",
        "pessoal",
        "termo",
        "numero_tarefa",
        "duracao_minutos",
    ],
    "additionalProperties": False,
}
