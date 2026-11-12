# Avaliação da extração de eventos

Mede quanto o modelo acerta ao transformar uma mensagem em texto livre nos campos do
evento (título, data, horários, responsável e recorrência).

- `casos.json`: 40 mensagens fictícias com a resposta esperada, separadas em grupos
  (datas relativas, horários informais, intervalos, recorrências, responsável). A data de
  referência é fixa (quinta-feira, 12/11/2026, 10h), então o gabarito não muda com o dia.
- `run_eval.py`: chama `bot.extrair_eventos` com o mesmo prompt e o mesmo schema de
  produção, congela a data, guarda a resposta crua do modelo e o tempo de cada chamada.
- `pontuacao.py`: compara campo a campo. O título está certo se contém uma das
  palavras-chave do gabarito; horas aceitam `9:00` e `09:00`; campos listados em
  `ignorar` não contam (por exemplo, a data inicial de uma recorrência sem início claro).
- `resultados/`: relatório em Markdown e JSON de cada execução.

O responsável é avaliado como o bot usa: quando o código reconhece o nome no texto, ele
sobrescreve o que o modelo devolveu.

## Rodar

Com o Ollama acessível (por exemplo, por túnel SSH para o container do servidor):

```bash
python -m evals.run_eval --ollama-url http://localhost:11435 --modelo gemma3:1b
python -m evals.run_eval --limite 5   # só os 5 primeiros casos
```

Os testes da pontuação rodam junto com os demais, sem rede:

```bash
python -m pytest tests/test_evals_pontuacao.py
```

## Resultados (gemma3:1b, servidor de 1 CPU)

| Conjunto | Só o modelo | Modelo + regras de `date_rules.py` |
|---|---|---|
| 40 casos de ajuste (`casos.json`) | 0/40 casos 100% corretos; data certa em 21% | 40/40 (otimista: as regras foram escritas olhando estes casos) |
| 15 casos de validação (`casos_novos.json`), escritos depois das regras | 0/15; data certa em 17% | 14/15: um caso ("todo último domingo do mês") tinha os campos certos, mas o fim de série curto devolvido pelo modelo zerava a recorrência; corrigido depois da medição |

Latência mediana de 17 a 19 s por mensagem. O erro mais comum do modelo era copiar a data
atual do prompt para o campo `data` e mover a data certa para `data_fim`, além de inventar
recorrência diária. Relatórios completos em `resultados/`.
