# Bot do Telegram — Agenda Compartilhada

Bot que recebe mensagens em texto livre no Telegram e cria eventos direto no Google Agenda compartilhado do Matheus e da Letícia.

**Documentação da rotina e das convenções (fonte da verdade):** `~/Documents/agenda/README.md`
Esse bot segue as mesmas regras definidas lá:
- Fuso horário: `America/Boa_Vista` (UTC-4)
- Campo `description` do evento sempre com `Responsável: Matheus` / `Letícia` / `Ambos`
- Cores por responsável: 🔵 Azul (Matheus) · 🌸 Rosa (Letícia) · 🟢 Verde (Ambos) — mesma lógica do `colorizeRotina.gs`, mas aplicada direto na criação do evento (sem precisar rodar o script depois)

## Como funciona
1. Telegram recebe a mensagem (ex: "lembrar de pagar a conta de luz sexta às 18h")
2. O texto vai pra API do Gemini, que extrai título/data/hora/responsável em JSON
3. O bot cria o evento via Google Calendar API, já com a cor e a descrição certas

## Setup

### 1. Bot do Telegram
Fale com [@BotFather](https://t.me/BotFather) no Telegram → `/newbot` → guarde o token.

### 2. Chave da API do Gemini
aistudio.google.com → "Get API key" (tier gratuito cobre o uso pessoal).

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
# editar .env com o token do Telegram, a chave do Gemini e o e-mail do calendário
```

### 5. Testar
```bash
python3 bot.py
```
Manda uma mensagem de teste pro bot no Telegram e confere se o evento aparece no calendário.

### 6. Deixar rodando na VPS (systemd)
1. Copiar a pasta inteira pra VPS
2. Ajustar os caminhos e o usuário em `systemd/agenda-bot.service`
3. ```bash
   sudo cp systemd/agenda-bot.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now agenda-bot
   ```
4. Ver logs: `journalctl -u agenda-bot -f`

## Limitações conhecidas (v1)
- Não edita nem apaga eventos, só cria
- Não lida com eventos recorrentes (cada mensagem cria um evento único)
- Se o Gemini não conseguir extrair data/hora da mensagem, o bot responde com erro em vez de pedir mais detalhes
