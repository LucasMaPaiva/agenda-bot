"""Jobs do Telegram para notificações de eventos e resumos programados."""

import asyncio
import logging
import secrets
from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from agenda_bot.reminders import reminder_heading, reminder_kind


LOGGER = logging.getLogger(__name__)


async def check_event_reminders(app, context) -> None:
    try:
        now = app.datetime.now(app.TZ)
        with app.conectar_banco() as connection:
            users = connection.execute("SELECT role, chat_id FROM usuarios").fetchall()
            saved_settings = connection.execute(
                """
                SELECT role, chave, valor FROM configuracoes
                WHERE chave IN (
                    'lembretes_ativos',
                    'lembrete_normal_minutos',
                    'lembrete_preparo_minutos',
                    'lembrete_deslocamento_minutos'
                )
                """
            ).fetchall()
        settings_by_role = {}
        for item in saved_settings:
            settings_by_role.setdefault(item["role"], {})[item["chave"]] = item[
                "valor"
            ]
        settings = {
            user["role"]: app.configuracao_lembretes(
                user["role"], settings_by_role.get(user["role"], {})
            )
            for user in users
        }
        active_users = {
            user["role"]: user
            for user in users
            if settings[user["role"]]["ativo"]
        }
        if not active_users:
            app.limpar_notificacoes_antigas()
            app.set_reminder_status(now, None)
            return

        max_lead_time = max(
            max(config["normal"], config["preparo"], config["deslocamento"])
            for role, config in settings.items()
            if role in active_users
        )
        events = app.buscar_eventos(now, now + timedelta(minutes=max_lead_time))
        for event in events:
            start = app.inicio_do_evento(event)
            event_id = event.get("id")
            if not event_id or not start or start < now:
                continue
            start_iso = start.isoformat()
            responsible = app.responsavel_do_evento(event)
            kind = reminder_kind(event)
            seconds_until_start = max(0, int((start - now).total_seconds()))
            minutes_until_start = max(1, (seconds_until_start + 59) // 60)
            roles = ("Matheus", "Letícia") if responsible == "Ambos" else (responsible,)
            for role in roles:
                user = active_users.get(role)
                if not user:
                    continue
                lead_time = settings[role][kind]
                if seconds_until_start > lead_time * 60:
                    continue
                chat_id = user["chat_id"]
                if app.notificacao_enviada(event_id, start_iso, chat_id):
                    continue
                title = event.get("summary", "Sem título")[:120]
                message = (
                    f"{reminder_heading(kind)}: começa em {minutes_until_start} min\n"
                    f"📅 {title}\n"
                    f"🕒 {start.strftime('%d/%m às %H:%M')}\n"
                    f"👤 {responsible}"
                )
                location = event.get("location", "").strip()
                if location:
                    message += f"\n📍 {location[:160]}"
                token = secrets.token_hex(4)
                pending = context.application.bot_data.setdefault(
                    "lembretes_interativos", {}
                )
                pending[token] = {"chat_id": chat_id, "mensagem": message}
                while len(pending) > 100:
                    pending.pop(next(iter(pending)))
                buttons = [
                    [
                        InlineKeyboardButton("✅ Ciente", callback_data=f"la:{token}"),
                        InlineKeyboardButton("⏰ +10 min", callback_data=f"lr:{token}"),
                    ]
                ]
                if event.get("htmlLink"):
                    buttons.append(
                        [
                            InlineKeyboardButton(
                                "Abrir no Google Agenda", url=event["htmlLink"]
                            )
                        ]
                    )
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
                app.marcar_notificacao_enviada(
                    event_id, start_iso, chat_id, lead_time
                )
        app.limpar_notificacoes_antigas()
        app.set_reminder_status(app.datetime.now(app.TZ), None)
    except Exception as error:
        app.set_reminder_status(app.datetime.now(app.TZ), str(error))
        LOGGER.exception("Falha ao verificar lembretes")


async def send_snoozed_reminder(context) -> None:
    await context.bot.send_message(
        chat_id=context.job.data["chat_id"],
        text="⏰ Lembrete adiado:\n" + context.job.data["mensagem"],
    )


async def reminder_callback(app, update, context) -> None:
    query = update.callback_query
    if not app.role_do_usuario(query.from_user.id):
        await query.answer("Telegram não autorizado.", show_alert=True)
        return
    action, token = query.data.split(":", 1)
    pending = context.application.bot_data.get("lembretes_interativos", {})
    data = pending.pop(token, None)
    if not data:
        await query.answer("Este lembrete expirou.", show_alert=True)
        return
    await query.answer()
    if action == "lr":
        context.job_queue.run_once(
            send_snoozed_reminder,
            when=timedelta(minutes=10),
            data=data,
            name=f"lembrete-adiado-{token}",
        )
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("👍 Vou lembrar novamente em 10 minutos.")
    else:
        await query.edit_message_reply_markup(reply_markup=None)


async def check_personal_reminders(app, context) -> None:
    now = app.datetime.now(app.TZ)
    try:
        due_reminders = app.reivindicar_lembretes_avulsos_vencidos(now)
    except Exception:
        LOGGER.exception("Falha ao buscar lembretes avulsos vencidos")
        return
    for reminder in due_reminders:
        try:
            await context.bot.send_message(
                chat_id=reminder["chat_id"],
                text="🔔 Lembrete:\n" + reminder["texto"],
            )
        except Exception:
            app.liberar_lembrete_avulso(reminder["id"])
            LOGGER.exception("Falha ao enviar lembrete avulso %s", reminder["id"])
            continue
        try:
            app.concluir_lembrete_avulso(reminder["id"], now)
        except Exception:
            LOGGER.exception(
                "Lembrete avulso %s foi enviado, mas não pôde ser finalizado",
                reminder["id"],
            )


async def check_daily_summaries(app, context) -> None:
    try:
        now = app.datetime.now(app.TZ)
        with app.conectar_banco() as connection:
            users = connection.execute("SELECT role, chat_id FROM usuarios").fetchall()
        scheduled = []
        for user in users:
            role = user["role"]
            for kind, setting_key in (
                ("manha", "resumo_diario"),
                ("noite", "resumo_noturno"),
            ):
                configured_time = app.obter_configuracao(
                    role, setting_key, "desligado"
                )
                if configured_time != now.strftime("%H:%M"):
                    continue
                if app.resumo_proativo_enviado(role, now.date(), kind):
                    continue
                scheduled.append((user, kind))

        events_by_date = {}
        for user, kind in scheduled:
            role = user["role"]
            target_date = (
                now.date() if kind == "manha" else now.date() + timedelta(days=1)
            )
            try:
                if target_date not in events_by_date:
                    start = app.datetime.combine(
                        target_date, app.datetime.min.time(), tzinfo=app.TZ
                    )
                    events_by_date[target_date] = app.buscar_eventos(
                        start, start + timedelta(days=1)
                    )
                message = await asyncio.to_thread(
                    app.montar_resumo_proativo,
                    role,
                    kind,
                    now,
                    events_by_date[target_date],
                )
                if message:
                    await context.bot.send_message(
                        chat_id=user["chat_id"], text=message
                    )
                app.marcar_resumo_proativo_enviado(role, now.date(), kind)
            except Exception:
                LOGGER.exception("Falha no resumo %s de %s", kind, role)
        if scheduled:
            app.limpar_resumos_proativos_antigos()
    except Exception:
        LOGGER.exception("Falha ao enviar resumo diário")


async def configure_jobs(app, application) -> None:
    if application.job_queue is None:
        raise RuntimeError('Instale a dependência "python-telegram-bot[job-queue]"')
    application.job_queue.run_repeating(
        app.verificar_lembretes,
        interval=app.REMINDER_CHECK_SECONDS,
        first=5,
        name="lembretes-agenda",
    )
    application.job_queue.run_repeating(
        app.verificar_resumos_diarios,
        interval=60,
        first=10,
        name="resumos-diarios",
    )
    application.job_queue.run_repeating(
        app.verificar_lembretes_avulsos,
        interval=60,
        first=15,
        name="lembretes-avulsos",
    )
