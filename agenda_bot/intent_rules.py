"""Regras determinísticas para reconhecer pedidos em linguagem natural."""

import re
from datetime import date, datetime, timedelta
from typing import Optional
from unicodedata import combining, normalize

from agenda_bot.settings import TZ


def texto_normalizado(texto: str) -> str:
    return "".join(
        caractere
        for caractere in normalize("NFD", texto.casefold())
        if not combining(caractere)
    )


def periodo_do_texto(texto: str) -> Optional[str]:
    """Retorna o período do dia mencionado explicitamente pelo usuário."""
    palavras = set(re.findall(r"[a-z0-9]+", texto_normalizado(texto)))
    if palavras & {"madrugada"}:
        return "madrugada"
    if palavras & {"manha", "matutino"}:
        return "manhã"
    if palavras & {"tarde", "vespertino"}:
        return "tarde"
    if palavras & {"noite", "noturno"}:
        return "noite"
    return None


def resolver_data_curta(texto: str) -> date:
    normalizado = texto_normalizado(texto).strip()
    hoje = datetime.now(TZ).date()
    if normalizado in {"hoje", "hj"}:
        return hoje
    if normalizado in {"amanha", "amanhã"}:
        return hoje + timedelta(days=1)
    correspondencia = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", normalizado)
    if correspondencia:
        dia, mes, ano = correspondencia.groups()
        ano_numero = int(ano) if ano else hoje.year
        if ano and len(ano) == 2:
            ano_numero += 2000
        resultado = date(ano_numero, int(mes), int(dia))
        if not ano and resultado < hoje:
            resultado = resultado.replace(year=hoje.year + 1)
        return resultado
    dias = {
        "segunda": 0,
        "terca": 1,
        "quarta": 2,
        "quinta": 3,
        "sexta": 4,
        "sabado": 5,
        "domingo": 6,
    }
    if normalizado in dias:
        deslocamento = (dias[normalizado] - hoje.weekday()) % 7
        return hoje + timedelta(days=deslocamento)
    raise ValueError("use hoje, amanhã, um dia da semana ou uma data DD/MM")


def pedido_horarios_livres(texto: str) -> Optional[tuple[date, int]]:
    normalizado = texto_normalizado(texto)
    sinais = (
        "horario livre",
        "horarios livres",
        "onde cabe",
        "quando cabe",
        "posso encaixar",
        "da pra encaixar",
        "da para encaixar",
        "tenho tempo",
    )
    if not any(sinal in normalizado for sinal in sinais):
        return None

    data_consulta = None
    for termo in re.findall(
        r"\b(?:hoje|hj|amanha|segunda|terca|quarta|quinta|sexta|sabado|domingo|"
        r"\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b",
        normalizado,
    ):
        try:
            data_consulta = resolver_data_curta(termo)
            break
        except ValueError:
            continue
    if data_consulta is None:
        data_consulta = datetime.now(TZ).date()

    duracao = 60
    horas = re.search(
        r"\b(\d{1,2})\s*(?:h|hora|horas)"
        r"(?:\s*(?:e\s*)?(\d{1,2})(?:\s*min)?)?",
        normalizado,
    )
    minutos = re.search(r"\b(\d{1,3})\s*(?:min|minuto|minutos)\b", normalizado)
    if horas:
        duracao = int(horas.group(1)) * 60 + int(horas.group(2) or 0)
    elif minutos:
        duracao = int(minutos.group(1))
    return data_consulta, duracao


def pedido_planejamento_diario(texto: str) -> Optional[date]:
    normalizado = texto_normalizado(texto)
    sinais = (
        "organiza meu dia",
        "organize meu dia",
        "planeja meu dia",
        "planeje meu dia",
        "planejamento do dia",
        "monta meu dia",
        "monte meu dia",
    )
    if not any(sinal in normalizado for sinal in sinais):
        return None
    match = re.search(
        r"\b(hoje|hj|amanha|segunda|terca|quarta|quinta|sexta|sabado|domingo|"
        r"\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b",
        normalizado,
    )
    return resolver_data_curta(match.group(1)) if match else datetime.now(TZ).date()


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


def eh_consulta_de_compromisso_especifico(texto: str) -> bool:
    palavras = set(re.findall(r"[a-z0-9]+", texto_normalizado(texto)))
    return bool(
        palavras & {"hora", "horario", "horas", "quando"}
        and not eh_pedido_criacao_evento(texto)
        and not eh_pedido_edicao(texto)
    )


def eh_pedido_replicacao(texto: str) -> bool:
    palavras = set(re.findall(r"[a-z0-9]+", texto_normalizado(texto)))
    return bool(
        palavras
        & {
            "copia",
            "copiar",
            "copie",
            "duplica",
            "duplicar",
            "duplique",
            "repete",
            "repetir",
            "repita",
            "replica",
            "replicar",
            "replique",
        }
    )


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
    termos_agenda = {
        "agenda",
        "agendas",
        "compromisso",
        "compromissos",
        "evento",
        "eventos",
    }
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
    return bool(conjunto & periodos and conjunto <= palavras_de_consulta_curta)


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
        return None
    return hoje, hoje + timedelta(days=1), "hoje"
