"""Resolve no código as datas, horários e recorrências que estão explícitos no texto.

O modelo de linguagem continua extraindo o título e o que estas regras não cobrem, mas
o que dá para calcular de forma determinística não fica na mão dele. A avaliação em
``evals/`` mostrou que o modelo local pequeno confunde a data atual do prompt com a
data do evento e inventa recorrências.
"""

import re
import unicodedata
from calendar import monthrange
from datetime import date, timedelta

DIAS_SEMANA = {
    "segunda": 0,
    "terca": 1,
    "quarta": 2,
    "quinta": 3,
    "sexta": 4,
    "sabado": 5,
    "domingo": 6,
}
MESES = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}
_DIA = r"(segunda|terca|quarta|quinta|sexta|sabado|domingo)(?:-feira)?"


def normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto)
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", sem_acento.casefold())


def _proximo_dia_semana(hoje: date, dia: int, semana_que_vem: bool) -> date:
    if semana_que_vem:
        segunda_seguinte = hoje + timedelta(days=7 - hoje.weekday())
        return segunda_seguinte + timedelta(days=dia)
    dias = (dia - hoje.weekday()) % 7
    return hoje + timedelta(days=dias or 7)


def _dia_do_mes(hoje: date, dia: int) -> date | None:
    """"Dia 20" é deste mês; se já passou, é do mês seguinte."""
    ano, mes = hoje.year, hoje.month
    if dia < hoje.day:
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    if dia > monthrange(ano, mes)[1]:
        return None
    return date(ano, mes, dia)


def _data_com_mes(hoje: date, dia: int, mes: int, ano: int | None) -> date | None:
    if ano is None:
        ano = hoje.year if (mes, dia) >= (hoje.month, hoje.day) else hoje.year + 1
    elif ano < 100:
        ano += 2000
    try:
        return date(ano, mes, dia)
    except ValueError:
        return None


def datas_no_texto(texto: str, hoje: date) -> list[date]:
    """Datas explícitas na ordem em que aparecem no texto."""
    t = normalizar(texto)
    encontradas: list[tuple[int, date]] = []
    padroes = [
        (r"\bdepois de amanha\b", lambda m: hoje + timedelta(days=2)),
        (r"\bamanha\b", lambda m: hoje + timedelta(days=1)),
        (r"\b(?:hoje|hj)\b", lambda m: hoje),
        (
            rf"\b{_DIA}(\s+(?:da\s+)?semana\s+que\s+vem)?",
            lambda m: _proximo_dia_semana(hoje, DIAS_SEMANA[m.group(1)], bool(m.group(2))),
        ),
        (
            r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b",
            lambda m: _data_com_mes(
                hoje, int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None
            ),
        ),
        (
            r"\b(?:dia\s+)?(\d{1,2})o?\s+de\s+(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[a-z]*\b",
            lambda m: _data_com_mes(hoje, int(m.group(1)), MESES[m.group(2)], None),
        ),
        (r"\bdia\s+(\d{1,2})\b(?!\s*/|o?\s+de\s)", lambda m: _dia_do_mes(hoje, int(m.group(1)))),
    ]
    ocupado: list[tuple[int, int]] = []
    for padrao, resolver in padroes:
        for m in re.finditer(padrao, t):
            if any(a <= m.start() < b for a, b in ocupado):
                continue
            # "amanha" dentro de "depois de amanha" já foi tratado.
            data = resolver(m)
            ocupado.append((m.start(), m.end()))
            if data is not None:
                encontradas.append((m.start(), data))
    return [data for _, data in sorted(encontradas)]


def texto_tem_recorrencia(texto: str) -> bool:
    t = normalizar(texto)
    return bool(
        re.search(r"\b(?:todo|toda|todos|todas)\b", t)
        or re.search(rf"\bde {_DIA} a {_DIA}", t)
        or re.search(rf"\b{_DIA} e {_DIA}", t)
    )


def horarios_no_texto(texto: str) -> list[str]:
    """Horários explícitos (HH:MM) na ordem em que aparecem."""
    t = normalizar(texto)
    encontrados: list[tuple[int, str]] = []

    def adicionar(posicao: int, hora: int, minuto: int = 0) -> None:
        if 0 <= hora <= 23 and 0 <= minuto <= 59:
            encontrados.append((posicao, f"{hora:02d}:{minuto:02d}"))

    for m in re.finditer(r"\bmeio-dia\b", t):
        adicionar(m.start(), 12)
    for m in re.finditer(r"\bmeia-noite\b", t):
        adicionar(m.start(), 0)
    for m in re.finditer(r"\b(\d{1,2})(?:h(\d{2})?|:(\d{2}))(?![\d/])", t):
        adicionar(m.start(), int(m.group(1)), int(m.group(2) or m.group(3) or 0))
    for m in re.finditer(r"\b(?:as|das|ate)\s+(\d{1,2})(?![\d:h/])(\s+da\s+(manha|tarde|noite))?", t):
        hora = int(m.group(1))
        if m.group(3) in {"tarde", "noite"} and hora < 12:
            hora += 12
        adicionar(m.start(1), hora)
    for m in re.finditer(r"\b(\d{1,2})\s+da\s+(tarde|noite)\b", t):
        if not any(p == m.start() for p, _ in encontrados):
            hora = int(m.group(1))
            adicionar(m.start(), hora + 12 if hora < 12 else hora)
    vistos, resultado = set(), []
    for posicao, hora in sorted(encontrados):
        if posicao not in vistos:
            vistos.add(posicao)
            resultado.append(hora)
    return resultado


def corrigir_extracao(dados: dict, texto: str, hoje: date) -> dict:
    """Sobrescreve na saída do modelo o que o texto define de forma explícita."""
    dados = dict(dados)
    recorrente = texto_tem_recorrencia(texto)
    datas = datas_no_texto(texto, hoje)
    horarios = horarios_no_texto(texto)

    if not recorrente and datas:
        # Texto com data explícita e sem palavra de repetição: evento único ou intervalo.
        dados["recorrencia"] = "nenhuma"
        dados["dias_semana"] = []
        dados["ordem_mes"] = None
        if len(datas) >= 2:
            dados["data"], dados["data_fim"] = datas[0].isoformat(), datas[-1].isoformat()
        else:
            dados["data"], dados["data_fim"] = datas[0].isoformat(), None
    elif recorrente:
        dados.update(_recorrencia_no_texto(texto, dados))
        if not re.search(r"\bate\b", normalizar(texto)):
            # Sem "até" no texto, o fim da série é o padrão do bot, não um palpite do modelo
            # (a validação pegou um fim curto demais que zerava a recorrência mensal).
            dados["data_fim"] = None

    if horarios:
        dados["hora_inicio"] = horarios[0]
        dados["hora_fim"] = horarios[1] if len(horarios) >= 2 else None
    else:
        for campo in ("hora_inicio", "hora_fim"):
            valor = dados.get(campo)
            achado = re.search(r"(\d{1,2}):(\d{2})", valor) if isinstance(valor, str) else None
            if achado:
                dados[campo] = f"{int(achado.group(1)):02d}:{achado.group(2)}"
            elif campo == "hora_fim":
                dados[campo] = None
    return dados


ORDENS = {"primeiro": 1, "primeira": 1, "segundo": 2, "segunda": 2, "terceiro": 3,
          "terceira": 3, "quarto": 4, "quarta": 4, "ultimo": -1, "ultima": -1}


def _recorrencia_no_texto(texto: str, dados: dict) -> dict:
    """Tipo de recorrência, dias e ordem no mês quando o texto os define."""
    t = normalizar(texto)
    if re.search(r"\btodos os dias\b|\btodo dia\b(?!\s+\d)|\bdiariamente\b", t):
        return {"recorrencia": "diaria", "dias_semana": [], "ordem_mes": None}
    nomes = list(DIAS_SEMANA)
    intervalo = re.search(rf"\bde {_DIA} a {_DIA}", t)
    if intervalo:
        inicio, fim = DIAS_SEMANA[intervalo.group(1)], DIAS_SEMANA[intervalo.group(2)]
        dias = [nomes[i] for i in range(inicio, fim + 1)] if inicio <= fim else []
    else:
        dias = []
        for m in re.finditer(rf"\b{_DIA}", t):
            if m.group(1) not in dias:
                dias.append(m.group(1))
    mensal = re.search(
        rf"\b(primeir[oa]|segund[oa]|terceir[oa]|quart[oa]|ultim[oa])\s+{_DIA}\s+do\s+mes", t
    )
    if mensal:
        return {"recorrencia": "mensal", "dias_semana": [mensal.group(2)],
                "ordem_mes": ORDENS[mensal.group(1)]}
    if dias:
        return {"recorrencia": "semanal", "dias_semana": dias, "ordem_mes": None}
    return {}
