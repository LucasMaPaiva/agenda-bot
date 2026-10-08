"""Fluxo de criação e confirmação de lembretes avulsos no Telegram."""

import secrets
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from agenda_bot.personal_reminders import apply_update, format_preview, parse_request


PENDING_KEY = "rascunhos_lembretes_avulsos"
INPUT_KEY = "entrada_lembrete_avulso"
MAX_AGE_SECONDS = 900


def _get_draft(context, token: str) -> dict | None:
    pending = context.user_data.setdefault(PENDING_KEY, {})
    draft = pending.get(token)
    if not draft:
        return None
    if datetime.now().timestamp() - draft["created_at"] > MAX_AGE_SECONDS:
        pending.pop(token, None)
        return None
    return draft


def _missing_prompt(draft: dict) -> str | None:
    if not draft.get("items"):
        return "O que você quer que eu lembre? Pode mandar vários itens juntos."
    if draft.get("invalid_date"):
        return "Não entendi esse dia. Diga hoje, amanhã, um dia da semana ou uma data como 15/10."
    if draft.get("invalid_time"):
        return "Não entendi esse horário. Diga, por exemplo, 12h ou 12:30."
    if not draft.get("time"):
        if draft.get("date"):
            return "Que horas devo te lembrar? Por exemplo: 12h ou 12:30."
        return "Para qual dia e horário? Por exemplo: hoje às 12 ou amanhã às 12:30."
    if not draft.get("date"):
        return "Esse horário de hoje já passou. Para qual dia devo agendar?"
    if not draft.get("due_at"):
        return "O horário ficou no passado. Me diga um dia e horário futuros."
    return None


async def _show_confirmation(app, update, context, draft: dict) -> None:
    pending = context.user_data.setdefault(PENDING_KEY, {})
    now = datetime.now().timestamp()
    for old_token, old_draft in list(pending.items()):
        if now - old_draft.get("created_at", 0) > MAX_AGE_SECONDS:
            pending.pop(old_token, None)
    while len(pending) >= 10:
        oldest = min(pending, key=lambda item: pending[item].get("created_at", 0))
        pending.pop(oldest, None)
    token = secrets.token_hex(4)
    draft["created_at"] = now
    draft["stage"] = "confirm"
    pending[token] = draft
    context.user_data.pop(INPUT_KEY, None)
    await update.message.reply_text(
        format_preview(draft),
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Confirmar", callback_data=f"prc:{token}")],
                [InlineKeyboardButton("✏️ Ajustar", callback_data=f"pre:{token}")],
                [InlineKeyboardButton("Cancelar", callback_data=f"prd:{token}")],
            ]
        ),
    )


async def begin(app, update, context, role: str, text: str) -> None:
    draft = parse_request(text, app.datetime.now(app.TZ))
    if draft.get("time") and not draft.get("date") and not draft.get("invalid_date"):
        draft["date"] = app.datetime.now(app.TZ).date()
    if draft.get("due_at") and draft["due_at"] <= app.datetime.now(app.TZ):
        draft["date"] = None
        draft["due_at"] = None
    draft["role"] = role
    draft["chat_id"] = update.effective_chat.id
    draft["created_at"] = app.datetime.now().timestamp()
    context.user_data[INPUT_KEY] = draft
    prompt = _missing_prompt(draft)
    if prompt:
        await update.message.reply_text(prompt)
        return
    await _show_confirmation(app, update, context, draft)


async def continue_input(app, update, context, role: str, text: str) -> bool:
    draft = context.user_data.get(INPUT_KEY)
    if not draft or draft.get("role") != role:
        return False
    normalized = app.texto_normalizado(text)
    if normalized in {"cancela", "cancelar", "deixa", "deixa pra la", "nao"}:
        context.user_data.pop(INPUT_KEY, None)
        await update.message.reply_text("Tudo bem, não criei o lembrete.")
        return True

    update_data = parse_request(text, app.datetime.now(app.TZ))
    if update_data.get("date"):
        draft["invalid_date"] = False
    elif update_data.get("invalid_date"):
        draft["invalid_date"] = True
    if update_data.get("time"):
        draft["invalid_time"] = False
    elif update_data.get("invalid_time"):
        draft["invalid_time"] = True
    if update_data.get("time"):
        draft["time"] = update_data["time"]
        if update_data.get("date"):
            draft["date"] = update_data["date"]
        elif not draft.get("date"):
            draft["date"] = app.datetime.now(app.TZ).date()
    elif update_data.get("date"):
        draft["date"] = update_data["date"]
    if update_data.get("items"):
        draft["items"] = update_data["items"]
    if draft.get("date") and draft.get("time"):
        draft["due_at"] = app.datetime.combine(
            draft["date"], draft["time"], tzinfo=app.TZ
        )
        if draft["due_at"] <= app.datetime.now(app.TZ):
            draft["date"] = None
            draft["due_at"] = None
    prompt = _missing_prompt(draft)
    if prompt:
        await update.message.reply_text(prompt)
    else:
        await _show_confirmation(app, update, context, draft)
    return True


async def edit_input(app, update, context, role: str, text: str) -> bool:
    token = context.user_data.get("lembrete_em_edicao")
    if not token:
        return False
    draft = _get_draft(context, token)
    context.user_data.pop("lembrete_em_edicao", None)
    if not draft or draft.get("role") != role:
        await update.message.reply_text("Essa edição expirou. Envie o lembrete novamente.")
        return True
    normalized = app.texto_normalizado(text)
    if normalized in {"cancela", "cancelar", "deixa", "deixa pra la", "nao"}:
        context.user_data[PENDING_KEY].pop(token, None)
        await update.message.reply_text("Tudo bem, não criei o lembrete.")
        return True
    revised = apply_update(draft, text, app.datetime.now(app.TZ))
    if revised.get("due_at") and revised["due_at"] <= app.datetime.now(app.TZ):
        revised["date"] = None
        revised["due_at"] = None
    revised["role"] = role
    revised["chat_id"] = draft["chat_id"]
    context.user_data[PENDING_KEY].pop(token, None)
    prompt = _missing_prompt(revised)
    if prompt:
        context.user_data[INPUT_KEY] = revised
        await update.message.reply_text(prompt)
    else:
        await _show_confirmation(app, update, context, revised)
    return True


async def callback(app, update, context) -> None:
    query = update.callback_query
    role = app.role_do_usuario(query.from_user.id)
    if not role:
        await query.answer("Telegram não autorizado.", show_alert=True)
        return
    action, token = query.data.split(":", 1)
    draft = _get_draft(context, token)
    if not draft or draft.get("role") != role:
        await query.answer("Esta confirmação expirou.", show_alert=True)
        await query.edit_message_text("⌛ Esta confirmação expirou. Envie o lembrete novamente.")
        return
    if action in {"prc", "prd"}:
        # Retire o token antes do primeiro await para impedir confirmação duplicada.
        context.user_data[PENDING_KEY].pop(token, None)
    await query.answer()
    if action == "prd":
        await query.edit_message_text("👍 Lembrete cancelado. Nada foi salvo.")
        return
    if action == "pre":
        context.user_data["lembrete_em_edicao"] = token
        await query.edit_message_text(
            f"{format_preview(draft)}\n\nMande a correção (ex.: ‘troca para 13h’ ou ‘tira encher o pneu’)."
        )
        return

    if draft["due_at"] <= app.datetime.now(app.TZ):
        draft["date"] = None
        draft["due_at"] = None
        context.user_data[INPUT_KEY] = draft
        await query.edit_message_text(
            "Esse horário já passou. Me diga um novo dia e horário para eu atualizar a confirmação."
        )
        return

    body = "\n".join(f"• {item}" for item in draft["items"])
    try:
        app.salvar_lembrete_avulso(role, draft["chat_id"], body, draft["due_at"])
    except Exception:
        context.user_data[PENDING_KEY][token] = draft
        await query.edit_message_text(
            "❌ Não consegui salvar agora. Você ainda pode tentar novamente.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ Tentar confirmar", callback_data=f"prc:{token}")]]
            ),
        )
        return
    await query.edit_message_text(f"✅ Lembrete confirmado:\n\n{format_preview(draft)}")
