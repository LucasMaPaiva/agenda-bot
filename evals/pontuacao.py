"""Compara a saída do modelo com o gabarito de cada caso, campo a campo."""

import unicodedata

CAMPOS = [
    "titulo",
    "data",
    "data_fim",
    "hora_inicio",
    "hora_fim",
    "responsavel",
    "recorrencia",
    "dias_semana",
    "ordem_mes",
]


def normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in sem_acento if not unicodedata.combining(c)).casefold()


def normalizar_hora(valor):
    """Aceita "9:00" e "09:00" como a mesma hora."""
    if not isinstance(valor, str) or ":" not in valor:
        return valor
    hora, minuto = valor.split(":", 1)
    if not (hora.isdigit() and minuto[:2].isdigit()):
        return valor
    return f"{int(hora):02d}:{minuto[:2]}"


def campo_correto(campo: str, esperado, obtido) -> bool:
    if campo == "titulo":
        # O título está certo se contém ao menos uma das palavras-chave do gabarito.
        if not isinstance(obtido, str):
            return False
        titulo = normalizar(obtido)
        return any(normalizar(palavra) in titulo for palavra in esperado)
    if campo in {"hora_inicio", "hora_fim"}:
        return normalizar_hora(esperado) == normalizar_hora(obtido)
    if campo == "dias_semana":
        return sorted(esperado or []) == sorted(obtido or [])
    return esperado == obtido


def avaliar_caso(caso: dict, obtido: dict | None) -> dict[str, bool]:
    """Devolve {campo: acertou} só para os campos que o caso avalia."""
    esperado = caso["esperado"]
    ignorar = set(caso.get("ignorar", []))
    resultado = {}
    for campo in CAMPOS:
        if campo in ignorar or campo not in esperado:
            continue
        resultado[campo] = obtido is not None and campo_correto(
            campo, esperado[campo], obtido.get(campo)
        )
    return resultado
