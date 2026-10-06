# Bot do Telegram — Agenda Compartilhada

Bot que recebe mensagens em texto livre no Telegram e cria eventos direto no Google Agenda compartilhado do Matheus e da Letícia.

Além de criar eventos, o bot consulta compromissos de hoje, amanhã ou da semana e
envia lembretes no Telegram. Somente os dois usuários cadastrados podem usar o bot.

**Documentação da rotina e das convenções (fonte da verdade):** `~/Documents/agenda/README.md`
Esse bot segue as mesmas regras definidas lá:
- Fuso horário: `America/Boa_Vista` (UTC-4)
- Campo `description` do evento sempre com `Responsável: Matheus` / `Letícia` / `Ambos`
- Cores por responsável: 🔵 Azul (Matheus) · 🌸 Rosa (Letícia) · 🟢 Verde (Ambos) — mesma lógica do `colorizeRotina.gs`, mas aplicada direto na criação do evento (sem precisar rodar o script depois)

## Como funciona
1. Telegram recebe a mensagem (ex: "lembrar de pagar a conta de luz sexta às 18h")
2. O texto vai pra API do Gemini, que extrai título/data/hora/responsável em JSON estruturado
3. O bot cria o evento via Google Calendar API, já com a cor e a descrição certas
4. A cada minuto, o bot procura compromissos próximos e avisa o responsável 30 minutos antes

Consultas como "o que tenho hoje?", "quais são meus compromissos de amanhã?" e
"mostre minha agenda desta semana" são respondidas sem criar eventos.

Pedidos como "cancelar academia de amanhã" ou `/cancelar academia amanhã` buscam
os eventos correspondentes. Se houver mais de um, o bot pede para escolher e sempre
exige confirmação antes de excluir. A confirmação expira após 15 minutos.

Intervalos como "Evangelismo de 10/10 até 18/10 às 19h20" criam um evento em
cada dia, incluindo a data inicial e a final. Por segurança, o limite padrão é de
31 dias e pode ser alterado com `MAX_EVENT_RANGE_DAYS`.

## Setup

### 1. Bot do Telegram
Fale com [@BotFather](https://t.me/BotFather) no Telegram → `/newbot` → guarde o token.

### 2. Chave da API do Gemini
aistudio.google.com → "Get API key" (tier gratuito cobre o uso pessoal).

O bot usa `gemini-3.6-flash` por padrão e tenta `gemini-3.5-flash` automaticamente
se o modelo principal estiver temporariamente indisponível. Para trocar os modelos
sem alterar o código, defina `GEMINI_MODEL` e `GEMINI_FALLBACK_MODEL` no `.env`.
Cada tentativa tem limite padrão de 30 segundos (`GEMINI_TIMEOUT_SECONDS`) e o bot
continua recebendo atualizações do Telegram enquanto aguarda a resposta do modelo.

### 3. Google Calendar — Service Account
1. console.cloud.google.com → criar um projeto (ou usar um existente)
2. Ativar a **Google Calendar API** nesse projeto
3. "Credenciais" → "Criar credenciais" → "Conta de serviço" → criar
4. Na conta de serviço criada → "Chaves" → "Adicionar chave" → JSON → baixar e salvar como `service-account.json` nesta pasta
5. Copiar o e-mail da conta de serviço (algo como `nome@projeto.iam.gserviceaccount.com`)
6. No Google Agenda (calendar.google.com), compartilhar o calendário principal com esse e-mail, permissão **"Fazer alterações em eventos"** — mesmo processo já usado para compartilhar com a Letícia

### 4. Configurar o projeto
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# editar .env com tokens, calendário e códigos diferentes para Matheus e Letícia
```

`DATABASE_PATH` guarda os cadastros e o histórico de lembretes enviados. Não apague
esse arquivo enquanto o bot estiver em uso.

### 5. Cadastrar os dois Telegrams

Com o bot rodando, cada pessoa abre uma conversa privada com ele e usa seu código:

```text
/registrar matheus CODIGO_DO_MATHEUS
/registrar leticia CODIGO_DA_LETICIA
```

O bot não recebe o número de telefone. O cadastro associa o identificador numérico e
estável que o Telegram entrega ao bot quando a pessoa envia a mensagem.

### 6. Testar localmente
```bash
python3 bot.py
```
Mande uma mensagem para criar um evento e teste uma consulta:

```text
Dentista amanhã às 9h, responsável Letícia
O que tenho amanhã?
```

Também existe o comando explícito `/agenda hoje`, `/agenda amanhã` ou `/agenda semana`.

### 7. Rodar com Docker

O container não expõe portas porque o bot usa polling do Telegram. A credencial da
conta de serviço é montada somente para leitura, e o banco SQLite fica persistido em
`./data`.

```bash
docker compose up --build -d
docker compose logs -f agenda-bot
```

O container usa `restart: unless-stopped`, nível de log `WARNING` e rotação limitada
a dois arquivos comprimidos de 5 MB. Ele continua rodando após fechar o terminal e
volta automaticamente depois de reiniciar o Docker ou o servidor.

Para aplicar uma alteração futura:

```bash
docker compose up --build -d
```

Para parar:

```bash
docker compose down
```

### 8. Deixar rodando na VPS sem Docker (systemd)
1. Copiar a pasta inteira pra VPS
2. Ajustar os caminhos e o usuário em `systemd/agenda-bot.service`
3. ```bash
   sudo cp systemd/agenda-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now agenda-bot
   ```
4. Ver logs: `journalctl -u agenda-bot -f`

## Limitações conhecidas (v1)
- Não edita eventos; o cancelamento exige confirmação no Telegram
- Intervalos são criados como eventos diários independentes, não como uma recorrência
  editável em bloco no Google Agenda
- Se o Gemini não conseguir extrair data/hora da mensagem, o bot responde com erro em vez de pedir mais detalhes
- Não adiciona convidados aos eventos: contas de serviço só podem preencher `attendees`
  com delegação de domínio do Google Workspace. O calendário compartilhado continua
  visível para Matheus e Letícia, e os lembretes de eventos "Ambos" vão para os dois.
