"""Avalia a extração de eventos do bot com mensagens de resposta conhecida.

Usa o mesmo prompt e o mesmo schema do bot (``bot.extrair_eventos``), com a data
congelada na referência do arquivo de casos, e mede acerto por campo e latência.

Exemplo (Ollama acessível em localhost:11435, por túnel SSH):

    python -m evals.run_eval --ollama-url http://localhost:11435 --modelo gemma3:1b
"""

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def configurar_ambiente(ollama_url: str, modelo: str) -> None:
    """Variáveis exigidas pelo bot. Nada aqui acessa Telegram ou Google."""
    os.environ.update(
        {
            "TELEGRAM_BOT_TOKEN": "eval",
            "CALENDAR_ID": "eval@example.com",
            "GEMINI_API_KEY": "",
            "OLLAMA_ENABLED": "true",
            "OLLAMA_BASE_URL": ollama_url,
            "OLLAMA_MODEL": modelo,
            "OLLAMA_TIMEOUT_SECONDS": "180",
            "OLLAMA_KEEP_ALIVE": "5m",
            "DATABASE_PATH": str(Path(tempfile.mkdtemp()) / "eval.db"),
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ollama-url", default="http://localhost:11435")
    parser.add_argument("--modelo", default="gemma3:1b")
    parser.add_argument("--casos", type=Path, default=RAIZ / "evals" / "casos.json")
    parser.add_argument("--saida", type=Path, default=RAIZ / "evals" / "resultados")
    parser.add_argument("--limite", type=int, default=0, help="roda só os N primeiros casos")
    parser.add_argument(
        "--reavaliar", type=Path,
        help="pontua de novo as respostas salvas num JSON de resultados, sem chamar o modelo",
    )
    args = parser.parse_args()

    configurar_ambiente(args.ollama_url, args.modelo)
    sys.path.insert(0, str(RAIZ))
    import bot  # noqa: E402  (precisa das variáveis acima)
    from evals.pontuacao import CAMPOS  # noqa: E402

    dados = json.loads(args.casos.read_text(encoding="utf-8"))
    referencia = datetime.fromisoformat(dados["referencia"]).replace(tzinfo=bot.TZ)

    class DataCongelada(datetime):
        @classmethod
        def now(cls, tz=None):
            return referencia if tz else referencia.replace(tzinfo=None)

    bot.datetime = DataCongelada

    # Guarda a resposta crua do modelo e o tempo de cada chamada.
    chamadas: list[dict] = []
    original = bot.gerar_json_operacao

    def gerar_e_medir(prompt, schema=bot.EVENTO_SCHEMA):
        inicio = time.perf_counter()
        bruto = original(prompt, schema)
        chamadas.append({"bruto": bruto, "segundos": time.perf_counter() - inicio})
        return bruto

    bot.gerar_json_operacao = gerar_e_medir

    casos = dados["casos"][: args.limite or None]
    resultados = []
    if args.reavaliar:
        salvos = {r["id"]: r for r in json.loads(args.reavaliar.read_text(encoding="utf-8"))}
        for caso in casos:
            salvo = salvos[caso["id"]]
            bruto = salvo.get("modelo", salvo.get("obtido"))
            erro = salvo["erro"] if bruto is None else None
            resultados.append(pontuar(bot, caso, bruto, referencia.date(), erro, salvo["segundos"]))
        casos = []
    for caso in casos:
        chamadas.clear()
        erro = None
        try:
            bot.extrair_eventos(caso["texto"])
        except Exception as exc:  # o caso conta como erro, a avaliação continua
            erro = f"{type(exc).__name__}: {exc}"
        modelo_json = None
        if chamadas:
            try:
                modelo_json = json.loads(chamadas[-1]["bruto"])
            except json.JSONDecodeError:
                erro = erro or "JSON inválido"
        resultados.append(
            pontuar(bot, caso, modelo_json, referencia.date(), erro,
                    round(sum(c["segundos"] for c in chamadas), 2))
        )
        notas = resultados[-1]["notas"]
        status = "ok " if notas and all(notas.values()) else "ERR"
        print(f"[{status}] {caso['id']:>2} {resultados[-1]['segundos']:>6.1f}s  {caso['texto']}")

    relatorio = montar_relatorio(resultados, CAMPOS, args.modelo, dados["referencia"])
    args.saida.mkdir(parents=True, exist_ok=True)
    carimbo = datetime.now().strftime("%Y%m%d-%H%M")
    base = args.saida / f"{args.modelo.replace(':', '-')}-{carimbo}"
    base.with_suffix(".json").write_text(
        json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    base.with_suffix(".md").write_text(relatorio, encoding="utf-8")
    print("\n" + relatorio)
    print(f"Arquivos: {base}.md e {base}.json")
    return 0


def pontuar(bot, caso, modelo_json, hoje, erro, segundos) -> dict:
    """Nota de dois jeitos: só a saída do modelo e a saída depois das regras do bot."""
    from agenda_bot.date_rules import corrigir_extracao
    from evals.pontuacao import avaliar_caso

    resolvido = bot.responsavel_mencionado_no_texto(caso["texto"], None)
    so_modelo = com_regras = None
    if modelo_json is not None:
        so_modelo = dict(modelo_json)
        com_regras = corrigir_extracao(modelo_json, caso["texto"], hoje)
        if resolvido:  # o bot sobrescreve o responsável quando reconhece o nome
            so_modelo["responsavel"] = com_regras["responsavel"] = resolvido
    return {
        "id": caso["id"],
        "grupo": caso["grupo"],
        "texto": caso["texto"],
        "esperado": caso["esperado"],
        "modelo": modelo_json,
        "obtido": com_regras,
        "notas_so_modelo": avaliar_caso(caso, so_modelo),
        "notas": avaliar_caso(caso, com_regras),
        "segundos": segundos,
        "erro": erro,
    }


def montar_relatorio(resultados, campos, modelo, referencia) -> str:
    total = len(resultados)
    perfeitos = sum(1 for r in resultados if r["notas"] and all(r["notas"].values()))
    tempos = [r["segundos"] for r in resultados if r["segundos"]]
    so_modelo = sum(
        1 for r in resultados if r["notas_so_modelo"] and all(r["notas_so_modelo"].values())
    )
    linhas = [
        f"# Avaliação da extração de eventos: {modelo}",
        "",
        f"Data de referência: {referencia}. Casos: {total}.",
        "",
        f"- Casos 100% corretos, só o modelo: **{so_modelo}/{total} ({so_modelo / total:.0%})**",
        f"- Casos 100% corretos, modelo + regras do bot: **{perfeitos}/{total} ({perfeitos / total:.0%})**",
    ]
    if tempos:
        p95 = sorted(tempos)[max(0, int(len(tempos) * 0.95) - 1)]
        linhas.append(
            f"- Latência: mediana {statistics.median(tempos):.1f} s, p95 {p95:.1f} s"
        )
    erros = [r for r in resultados if r["erro"]]
    linhas += [f"- Chamadas com erro: {len(erros)}", "", "## Acerto por campo", ""]
    linhas += ["| Campo | Só o modelo | Modelo + regras |", "|---|---|---|"]
    for campo in campos:
        antes = [r["notas_so_modelo"][campo] for r in resultados if campo in r["notas_so_modelo"]]
        depois = [r["notas"][campo] for r in resultados if campo in r["notas"]]
        if depois:
            linhas.append(
                f"| {campo} | {sum(antes)}/{len(antes)} ({sum(antes) / len(antes):.0%}) "
                f"| {sum(depois)}/{len(depois)} ({sum(depois) / len(depois):.0%}) |"
            )
    linhas += ["", "## Acerto por grupo", "", "| Grupo | Casos 100% | Total |", "|---|---|---|"]
    por_grupo = defaultdict(list)
    for r in resultados:
        por_grupo[r["grupo"]].append(bool(r["notas"]) and all(r["notas"].values()))
    for grupo, oks in sorted(por_grupo.items()):
        linhas.append(f"| {grupo} | {sum(oks)} | {len(oks)} |")
    linhas += ["", "## Erros", ""]
    for r in resultados:
        errados = [c for c, ok in r["notas"].items() if not ok]
        if not errados and not r["erro"]:
            continue
        detalhe = ", ".join(
            f"{c}: esperado {r['esperado'].get(c)!r}, veio {(r['obtido'] or {}).get(c)!r}"
            for c in errados
        )
        linhas.append(f"- **{r['id']}** \"{r['texto']}\": {r['erro'] or detalhe}")
    return "\n".join(linhas) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
