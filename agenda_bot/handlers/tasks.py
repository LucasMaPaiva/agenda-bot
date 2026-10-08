"""Handlers e confirmações de Google Tarefas."""

import asyncio
from datetime import date, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def task_summary(task: dict) -> str:
    due = task.get("prazo")
    formatted_due = date.fromisoformat(due).strftime("%d/%m/%Y") if due else "sem prazo"
    text = f"{task['titulo']}\n📅 {formatted_due}"
    if task.get("notas"):
        text += f"\n📝 {task['notas']}"
    return text


async def begin_task_creation(app, update, context, role: str, text: str) -> None:
    extracted = await asyncio.to_thread(app.extrair_tarefa, text)
    token = app.salvar_acao_pendente(
        context,
        "tarefas_pendentes",
        {"role": role, "tarefa": extracted},
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Criar tarefa", callback_data=f"tc:{token}")],
            [InlineKeyboardButton("Não criar", callback_data=f"ta:{token}")],
        ]
    )
    await update.message.reply_text(
        f"Confira a tarefa:\n\n{task_summary(extracted)}",
        reply_markup=keyboard,
    )


async def task_creation_callback(app, update, context) -> None:
    query = update.callback_query
    role = app.role_do_usuario(query.from_user.id)
    if not role:
        await query.answer("Telegram não autorizado.", show_alert=True)
        return
    await query.answer()
    action, token = query.data.split(":", 1)
    pending_items = context.user_data.get("tarefas_pendentes", {})
    pending = pending_items.get(token)
    if (
        not pending
        or datetime.now(app.TZ).timestamp() - pending["criado_em"] > 900
    ):
        pending_items.pop(token, None)
        await query.edit_message_text(
            "⌛ Esta confirmação expirou. Envie a tarefa novamente."
        )
        return
    if action == "ta":
        pending_items.pop(token, None)
        await query.edit_message_text("👍 Criação cancelada. Nada foi adicionado.")
        return
    if pending["role"] != role:
        await query.edit_message_text("❌ Esta tarefa pertence a outro usuário.")
        return
    try:
        await asyncio.to_thread(app.criar_tarefa_google, role, pending["tarefa"])
    except Exception as error:
        app.LOGGER.exception("Falha ao criar tarefa")
        await query.edit_message_text(app.mensagem_erro_usuario("criar a tarefa", error))
        return
    pending_items.pop(token, None)
    await query.edit_message_text(
        f"✅ Tarefa criada para {role}:\n\n{task_summary(pending['tarefa'])}"
    )


async def create_task_command(app, update, context) -> None:
    role = app.role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    if not context.args:
        await update.message.reply_text(
            "Use: /tarefa DESCRIÇÃO\nExemplo: /tarefa comprar sabão até sexta"
        )
        return
    try:
        await begin_task_creation(app, update, context, role, " ".join(context.args))
    except Exception as error:
        app.LOGGER.exception("Falha ao preparar tarefa")
        await update.message.reply_text(
            app.mensagem_erro_usuario("preparar a tarefa", error)
        )


async def list_tasks_command(app, update, context) -> None:
    role = app.role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    try:
        items = await asyncio.to_thread(app.listar_tarefas_google, role)
    except Exception as error:
        app.LOGGER.exception("Falha ao listar tarefas")
        await update.message.reply_text(
            app.mensagem_erro_usuario("listar suas tarefas", error)
        )
        return
    if not items:
        context.user_data["ultima_lista_tarefas"] = []
        await update.message.reply_text("✅ Você não tem tarefas pendentes.")
        return
    items = items[:30]
    context.user_data["ultima_lista_tarefas"] = [item["id"] for item in items]
    context.user_data["ultima_lista_tarefas_itens"] = items
    lines = [f"📋 Tarefas de {role}:"]
    for index, item in enumerate(items, start=1):
        due = item.get("due")
        due_text = (
            datetime.fromisoformat(due.replace("Z", "+00:00")).strftime("%d/%m")
            if due
            else "sem prazo"
        )
        title = item.get("title", "Sem título")
        if len(title) > 90:
            title = f"{title[:87]}…"
        lines.append(f"\n{index}. {title} — {due_text}")
    lines.append("\n\nPara concluir: /concluir NÚMERO")
    await update.message.reply_text("".join(lines))


async def complete_task_command(app, update, context) -> None:
    role = app.role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text(
            "Primeiro use /tarefas e depois /concluir NÚMERO."
        )
        return
    await complete_listed_task(app, update, context, role, int(context.args[0]))


async def complete_listed_task(app, update, context, role: str, number: int) -> None:
    index = number - 1
    ids = context.user_data.get("ultima_lista_tarefas", [])
    if index < 0 or index >= len(ids):
        await update.message.reply_text("Número inválido. Use /tarefas novamente.")
        return
    try:
        await asyncio.to_thread(app.concluir_tarefa_google, role, ids[index])
    except Exception as error:
        app.LOGGER.exception("Falha ao concluir tarefa")
        await update.message.reply_text(
            app.mensagem_erro_usuario("concluir a tarefa", error)
        )
        return
    await update.message.reply_text("✅ Tarefa concluída.")


async def edit_task_command(app, update, context) -> None:
    role = app.role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text(
            "Use /tarefas e depois: /editar_tarefa NÚMERO NOVO TÍTULO"
        )
        return
    index = int(context.args[0]) - 1
    items = context.user_data.get("ultima_lista_tarefas_itens", [])
    if index < 0 or index >= len(items):
        await update.message.reply_text("Número inválido. Use /tarefas novamente.")
        return
    new_title = " ".join(context.args[1:]).strip()
    token = app.salvar_acao_pendente(
        context,
        "alteracoes_tarefas_pendentes",
        {"role": role, "item": items[index], "novo_titulo": new_title},
    )
    await update.message.reply_text(
        f"Alterar tarefa:\n{items[index].get('title', 'Sem título')}\n→ {new_title}?",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Confirmar", callback_data=f"tuc:{token}")],
                [
                    InlineKeyboardButton(
                        "Não alterar", callback_data=f"tua:{token}"
                    )
                ],
            ]
        ),
    )


async def delete_task_command(app, update, context) -> None:
    role = app.role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text(
            "Use /tarefas e depois: /excluir_tarefa NÚMERO"
        )
        return
    index = int(context.args[0]) - 1
    items = context.user_data.get("ultima_lista_tarefas_itens", [])
    if index < 0 or index >= len(items):
        await update.message.reply_text("Número inválido. Use /tarefas novamente.")
        return
    token = app.salvar_acao_pendente(
        context,
        "exclusoes_tarefas_pendentes",
        {"role": role, "item": items[index]},
    )
    await update.message.reply_text(
        f"Excluir a tarefa “{items[index].get('title', 'Sem título')}”?",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Confirmar exclusão", callback_data=f"tdc:{token}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Não excluir", callback_data=f"tda:{token}"
                    )
                ],
            ]
        ),
    )


async def task_change_callback(app, update, context) -> None:
    query = update.callback_query
    role = app.role_do_usuario(query.from_user.id)
    if not role:
        await query.answer("Telegram não autorizado.", show_alert=True)
        return
    await query.answer()
    action, token = query.data.split(":", 1)
    key = (
        "alteracoes_tarefas_pendentes"
        if action.startswith("tu")
        else "exclusoes_tarefas_pendentes"
    )
    pending_items = context.user_data.get(key, {})
    pending = pending_items.get(token)
    expired = pending and (
        datetime.now(app.TZ).timestamp() - pending["criado_em"] > 900
    )
    if not pending or expired or pending["role"] != role:
        pending_items.pop(token, None)
        await query.edit_message_text("⌛ Esta confirmação expirou.")
        return
    if action in {"tua", "tda"}:
        pending_items.pop(token, None)
        await query.edit_message_text("👍 Nada foi alterado.")
        return
    item = pending["item"]
    try:
        if action == "tuc":
            await asyncio.to_thread(
                app.editar_tarefa_google,
                role,
                item["id"],
                pending["novo_titulo"],
            )
            message = "✅ Tarefa alterada."
        else:
            await asyncio.to_thread(app.excluir_tarefa_google, role, item["id"])
            message = "✅ Tarefa excluída."
    except Exception as error:
        operation = "editar a tarefa" if action == "tuc" else "excluir a tarefa"
        await query.edit_message_text(app.mensagem_erro_usuario(operation, error))
        return
    pending_items.pop(token, None)
    await query.edit_message_text(message)
