"""Ajuda resumida das funções do assistente no Telegram."""


FUNCTIONALITIES_MESSAGE = """🤖 O que posso fazer

📅 Agenda e planejamento
• Pergunte: “o que tenho hoje à noite?” ou “o que temos amanhã?”
• Crie eventos falando naturalmente; eu mostro uma prévia para confirmar.
• Consulte com /agenda hoje ou /agenda amanhã.
• Edite, cancele ou copie eventos: /editar, /cancelar e /replicar.
• Encontre horários livres com /livres amanhã 60.
• Organize o dia com /planejar amanhã; nada entra na agenda sem confirmação.

✅ Tarefas
• Crie uma tarefa com /tarefa comprar ração até sexta.
• Veja e atualize sua lista com /tarefas, /concluir, /editar_tarefa e /excluir_tarefa.
• Tarefas podem ter prazo ou ficar sem prazo.

💰 Finanças
• Registre um gasto dizendo “comprei uma coca por 24,50”. A IA local escolhe uma das
  suas categorias e só lança depois da sua confirmação.
• Pergunte “quais contas tenho para pagar?”, “qual é meu saldo?” ou “quanto está a fatura?”.

🔔 Lembretes
• Avulso: “me lembra amanhã às 12 de comprar ração e encher o pneu”.
  Eu confirmo com você; dá para ajustar ou cancelar antes de salvar.
• Eventos: os avisos chegam antes dos compromissos da agenda. Configure a antecedência
  com /lembretes.
• Resumo do dia: /resumo_diario 06:00; inclua também a prévia da noite com
  /resumo_diario 06:00 20:30.

⚙️ Preferências e rotinas
• Salve orientações com /preferencia e consulte com /preferencias.
• Salve, veja e aplique rotinas com /salvar_rotina, /rotinas e /aplicar_rotina.

Outros: /desfazer, /status, /versao e /limpar_conversa.

Você também pode falar comigo normalmente e continuar o assunto sem repetir tudo."""


async def show_functionalities(app, update, context) -> None:
    role = app.role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    await update.message.reply_text(FUNCTIONALITIES_MESSAGE)

