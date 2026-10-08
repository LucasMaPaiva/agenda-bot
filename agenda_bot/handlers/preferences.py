"""Handlers de preferências, planejamento, rotinas e automações pessoais."""

import asyncio
from datetime import datetime, timedelta


async def clear_conversation(app, update, context) -> None:
    role = app.role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    context.user_data.pop("historico_conversa", None)
    await asyncio.to_thread(app.apagar_conversa, role)
    await update.message.reply_text("🧹 Histórico e contexto da conversa limpos.")


async def save_preference(app, update, context) -> None:
    role = app.role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text(
            "Use: /preferencia TEXTO\n"
            "Exemplo: /preferencia Academia é prioridade de segunda a quinta"
        )
        return
    if len(text) > 300:
        await update.message.reply_text(
            "A preferência pode ter no máximo 300 caracteres."
        )
        return
    parts = text.split()
    if parts[0].casefold() == "buffer" and len(parts) == 2 and parts[1].isdigit():
        minutes = int(parts[1])
        if minutes < 0 or minutes > 120:
            await update.message.reply_text(
                "O buffer deve ficar entre 0 e 120 minutos."
            )
            return
        app.salvar_configuracao(role, "buffer_minutos", str(minutes))
        await update.message.reply_text(f"✅ Buffer salvo: {minutes} minutos.")
        return
    if parts[0].casefold() == "horario" and len(parts) == 3:
        try:
            day_start = datetime.strptime(parts[1], "%H:%M")
            day_end = datetime.strptime(parts[2], "%H:%M")
        except ValueError:
            await update.message.reply_text("Use: /preferencia horario 06:00 22:00")
            return
        if day_start >= day_end:
            await update.message.reply_text(
                "O início do dia deve ser anterior ao fim."
            )
            return
        app.salvar_configuracao(role, "inicio_dia", parts[1])
        app.salvar_configuracao(role, "fim_dia", parts[2])
        await update.message.reply_text(
            f"✅ Janela do dia salva: {parts[1]}–{parts[2]}."
        )
        return
    if (
        parts[0].casefold() in {"duracao_tarefa", "tarefa"}
        and len(parts) == 2
        and parts[1].isdigit()
    ):
        minutes = int(parts[1])
        if minutes < 15 or minutes > 120:
            await update.message.reply_text(
                "A duração padrão deve ficar entre 15 e 120 minutos."
            )
            return
        app.salvar_configuracao(role, "duracao_tarefa", str(minutes))
        await update.message.reply_text(
            f"✅ Duração padrão das tarefas: {minutes} minutos."
        )
        return
    if len(app.listar_preferencias(role)) >= 20:
        await update.message.reply_text(
            "Você já tem 20 preferências. Remova uma com /remover_preferencia."
        )
        return
    app.adicionar_preferencia(role, text)
    await update.message.reply_text("✅ Preferência salva e usada nas conversas.")


async def list_preferences(app, update, context) -> None:
    role = app.role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    items = app.listar_preferencias(role)
    lines = [f"⚙️ Preferências de {role}:"]
    if items:
        lines.extend(
            f"\n{index}. {item['texto']}" for index, item in enumerate(items, 1)
        )
    else:
        lines.append("\nNenhuma preferência textual salva.")
    lines.append(
        f"\n\nHorário: {app.obter_configuracao(role, 'inicio_dia', '06:00')}–"
        f"{app.obter_configuracao(role, 'fim_dia', '22:00')}"
        f"\nBuffer: {app.obter_configuracao(role, 'buffer_minutos', '15')} min"
        f"\nDuração das tarefas: "
        f"{app.obter_configuracao(role, 'duracao_tarefa', '30')} min"
    )
    await update.message.reply_text("".join(lines))


async def remove_preference(app, update, context) -> None:
    role = app.role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Use: /remover_preferencia NÚMERO")
        return
    if app.remover_preferencia(role, int(context.args[0])):
        await update.message.reply_text("✅ Preferência removida.")
    else:
        await update.message.reply_text("Número inválido. Use /preferencias.")


async def free_times(app, update, context) -> None:
    role = app.role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    if not context.args:
        await update.message.reply_text(
            "Use: /livres DATA [MINUTOS]\nExemplo: /livres amanhã 60"
        )
        return
    try:
        target_date = app.resolver_data_curta(context.args[0])
        duration = int(context.args[1]) if len(context.args) > 1 else 60
        windows = await asyncio.to_thread(
            app.calcular_horarios_livres, role, target_date, duration
        )
        await update.message.reply_text(
            app.formatar_horarios_livres(target_date, duration, windows)
        )
    except Exception as error:
        await update.message.reply_text(
            app.mensagem_erro_usuario("calcular horários livres", error)
        )


async def plan_day(app, update, context) -> None:
    role = app.role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    if len(context.args) > 1:
        await update.message.reply_text(
            "Use: /planejar hoje, /planejar amanhã ou /planejar DD/MM."
        )
        return
    try:
        target_date = (
            app.resolver_data_curta(context.args[0])
            if context.args
            else datetime.now(app.TZ).date()
        )
        await app.enviar_planejamento_diario(update, context, role, target_date)
    except Exception as error:
        app.LOGGER.exception("Falha ao montar planejamento diário")
        await update.message.reply_text(
            app.mensagem_erro_usuario("montar o planejamento", error)
        )


async def save_routine(app, update, context) -> None:
    role = app.role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    entry = " ".join(context.args)
    if "|" not in entry:
        await update.message.reply_text("Use: /salvar_rotina NOME | DATA")
        return
    name, date_text = (part.strip() for part in entry.split("|", 1))
    try:
        if not name or len(name) > 60:
            raise ValueError("o nome da rotina deve ter entre 1 e 60 caracteres")
        source_date = app.resolver_data_curta(date_text)
        start = datetime.combine(source_date, datetime.min.time(), tzinfo=app.TZ)
        events = await asyncio.to_thread(
            app.buscar_eventos, start, start + timedelta(days=1)
        )
        events = [
            event
            for event in events
            if app.responsavel_do_evento(event) in {role, "Ambos"}
            and app.inicio_do_evento(event)
            and app.fim_do_evento(event)
        ]
        if not events:
            raise ValueError("não encontrei eventos com horário nessa data")
        if len(events) > 30:
            raise ValueError("uma rotina pode conter no máximo 30 eventos")
        template = []
        for event in events:
            event_start = app.inicio_do_evento(event)
            event_end = app.fim_do_evento(event)
            template.append(
                {
                    "titulo": event.get("summary", "Sem título"),
                    "hora": event_start.strftime("%H:%M"),
                    "duracao": int((event_end - event_start).total_seconds() // 60),
                    "responsavel": app.responsavel_do_evento(event),
                }
            )
        app.salvar_rotina(role, name, template)
        await update.message.reply_text(
            f"✅ Rotina “{name}” salva com {len(template)} eventos."
        )
    except Exception as error:
        await update.message.reply_text(
            app.mensagem_erro_usuario("salvar a rotina", error)
        )


async def apply_routine(app, update, context) -> None:
    role = app.role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    entry = " ".join(context.args)
    if "|" not in entry:
        await update.message.reply_text("Use: /aplicar_rotina NOME | DATA")
        return
    name, date_text = (part.strip() for part in entry.split("|", 1))
    try:
        if not name:
            raise ValueError("informe o nome da rotina")
        template = app.obter_rotina(role, name)
        if not template:
            raise ValueError(f"a rotina “{name}” não existe")
        target = app.resolver_data_curta(date_text)
        events = []
        for item in template:
            start = datetime.combine(
                target,
                datetime.strptime(item["hora"], "%H:%M").time(),
                tzinfo=app.TZ,
            )
            events.append(
                {
                    "titulo": item["titulo"],
                    "inicio": start,
                    "fim": start + timedelta(minutes=item["duracao"]),
                    "responsavel": item["responsavel"],
                    "recorrencia": "nenhuma",
                }
            )
        conflicts = await asyncio.to_thread(app.conflitos_da_replicacao, events)
        await app.mostrar_confirmacao_criacao(
            update, context, events, f"Confira a rotina “{name}”:", conflicts
        )
    except Exception as error:
        await update.message.reply_text(
            app.mensagem_erro_usuario("aplicar a rotina", error)
        )


async def list_routines(app, update, context) -> None:
    role = app.role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    names = app.listar_rotinas(role)
    content = "\n".join(f"• {name}" for name in names) if names else "Nenhuma."
    await update.message.reply_text("📋 Rotinas salvas:\n" + content)


async def configure_daily_summary(app, update, context) -> None:
    role = app.role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    if not context.args:
        morning = app.obter_configuracao(role, "resumo_diario", "desligado")
        evening = app.obter_configuracao(role, "resumo_noturno", "desligado")
        await update.message.reply_text(
            f"Resumo da manhã: {morning}\nResumo da noite: {evening}\n\n"
            "Use /resumo_diario 06:00 20:30 ou /resumo_diario desligar."
        )
        return
    if len(context.args) == 1 and context.args[0].casefold() in {
        "desligar",
        "desligado",
        "off",
    }:
        app.salvar_configuracao(role, "resumo_diario", "desligado")
        app.salvar_configuracao(role, "resumo_noturno", "desligado")
        await update.message.reply_text("✅ Resumos proativos desligados.")
        return
    if len(context.args) not in {1, 2}:
        await update.message.reply_text(
            "Use /resumo_diario 06:00 20:30 ou /resumo_diario desligar."
        )
        return
    try:
        for value in context.args:
            datetime.strptime(value, "%H:%M")
    except ValueError:
        await update.message.reply_text(
            "Use horários HH:MM, por exemplo: /resumo_diario 06:00 20:30."
        )
        return
    morning = context.args[0]
    evening = context.args[1] if len(context.args) == 2 else "desligado"
    app.salvar_configuracao(role, "resumo_diario", morning)
    app.salvar_configuracao(role, "resumo_noturno", evening)
    message = f"✅ Resumo da manhã programado para {morning}."
    if evening != "desligado":
        message += f"\n✅ Prévia da noite programada para {evening}."
    await update.message.reply_text(message)


async def configure_reminders(app, update, context) -> None:
    role = app.role_do_usuario(update.effective_user.id)
    if not role:
        await update.message.reply_text("Telegram não autorizado. Envie /start.")
        return
    if not context.args:
        config = app.configuracao_lembretes(role)
        status = "ligados" if config["ativo"] else "desligados"
        await update.message.reply_text(
            f"🔔 Lembretes de {role}: {status}\n"
            f"Normal: {config['normal']} min antes\n"
            f"Preparação: {config['preparo']} min antes\n"
            f"Com localização: {config['deslocamento']} min antes\n\n"
            "Ajuste com /lembretes normal 30, /lembretes preparo 60 ou "
            "/lembretes deslocamento 45."
        )
        return
    if len(context.args) == 1 and context.args[0].casefold() in {
        "ligar",
        "ligado",
        "on",
        "desligar",
        "desligado",
        "off",
    }:
        enabled = context.args[0].casefold() in {"ligar", "ligado", "on"}
        app.salvar_configuracao(
            role, "lembretes_ativos", "sim" if enabled else "nao"
        )
        await update.message.reply_text(
            f"✅ Lembretes {'ligados' if enabled else 'desligados'} para {role}."
        )
        return
    aliases = {
        "normal": "normal",
        "preparo": "preparo",
        "preparacao": "preparo",
        "preparação": "preparo",
        "deslocamento": "deslocamento",
        "saida": "deslocamento",
        "saída": "deslocamento",
    }
    reminder_type = aliases.get(context.args[0].casefold()) if context.args else None
    if (
        len(context.args) != 2
        or not reminder_type
        or not context.args[1].isdigit()
    ):
        await update.message.reply_text(
            "Use: /lembretes normal 30, /lembretes preparo 60, "
            "/lembretes deslocamento 45 ou /lembretes desligar."
        )
        return
    minutes = int(context.args[1])
    if minutes < 5 or minutes > 180:
        await update.message.reply_text(
            "A antecedência deve ficar entre 5 e 180 minutos."
        )
        return
    app.salvar_configuracao(
        role, app.REMINDER_SETTING_KEYS[reminder_type], str(minutes)
    )
    names = {
        "normal": "normal",
        "preparo": "de preparação",
        "deslocamento": "com localização",
    }
    await update.message.reply_text(
        f"✅ Lembrete {names[reminder_type]} ajustado para {minutes} minutos antes."
    )
