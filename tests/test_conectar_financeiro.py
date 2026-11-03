import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.update(
    {
        "TELEGRAM_BOT_TOKEN": "123:test",
        "GEMINI_API_KEY": "test",
        "GOOGLE_SERVICE_ACCOUNT_FILE": "/tmp/not-used.json",
        "CALENDAR_ID": "calendar@example.com",
        "DATABASE_PATH": tempfile.mktemp(suffix=".db"),
    }
)

import bot  # noqa: E402
from agenda_bot.finance_gateway import FinanceGatewayError, FinanceLink  # noqa: E402


def mensagem():
    update = MagicMock()
    update.effective_user.id = 42
    update.effective_chat.id = 4242
    update.message.reply_text = AsyncMock()
    return update


def contexto():
    context = MagicMock()
    context.job_queue.get_jobs_by_name.return_value = []
    return context


class ConectarFinanceiroTest(unittest.TestCase):
    def test_manda_link_e_codigo_e_agenda_a_consulta(self):
        update, context = mensagem(), contexto()
        vinculo = FinanceLink("abc", "https://financeiro.example.com/conectar/telegram/abc", "839 340")

        with patch.object(bot, "role_do_usuario", return_value="Matheus"), patch.object(
            bot.finance_gateway, "can_link", return_value=True
        ), patch.object(bot.finance_gateway, "start_link", return_value=vinculo):
            asyncio.run(bot.conectar_financeiro_cmd(update, context))

        texto = update.message.reply_text.call_args.args[0]
        self.assertIn(vinculo.url, texto)
        self.assertIn("839 340", texto)
        agendamento = context.job_queue.run_repeating.call_args
        self.assertEqual(agendamento.kwargs["name"], "vinculo-financeiro:Matheus")
        self.assertEqual(
            agendamento.kwargs["data"], {"role": "Matheus", "code": "abc", "chat_id": 4242}
        )

    def test_recusa_quem_nao_esta_registrado(self):
        update, context = mensagem(), contexto()

        with patch.object(bot, "role_do_usuario", return_value=None):
            asyncio.run(bot.conectar_financeiro_cmd(update, context))

        update.message.reply_text.assert_awaited_once_with("Telegram não autorizado. Envie /start.")
        context.job_queue.run_repeating.assert_not_called()

    def test_avisa_quando_o_vinculo_nao_esta_configurado(self):
        update, context = mensagem(), contexto()

        with patch.object(bot, "role_do_usuario", return_value="Matheus"), patch.object(
            bot.finance_gateway, "can_link", return_value=False
        ):
            asyncio.run(bot.conectar_financeiro_cmd(update, context))

        self.assertIn("não está configurado", update.message.reply_text.call_args.args[0])
        context.job_queue.run_repeating.assert_not_called()

    def test_pedir_de_novo_cancela_a_consulta_anterior(self):
        update, context = mensagem(), contexto()
        anterior = MagicMock()
        context.job_queue.get_jobs_by_name.return_value = [anterior]

        with patch.object(bot, "role_do_usuario", return_value="Matheus"), patch.object(
            bot.finance_gateway, "can_link", return_value=True
        ), patch.object(
            bot.finance_gateway, "start_link", return_value=FinanceLink("x", "u", "000 000")
        ):
            asyncio.run(bot.conectar_financeiro_cmd(update, context))

        anterior.schedule_removal.assert_called_once()

    def _consulta(self, status=None, erro=None):
        context = MagicMock()
        context.job = SimpleNamespace(
            data={"role": "Matheus", "code": "abc", "chat_id": 4242},
            schedule_removal=MagicMock(),
        )
        context.bot.send_message = AsyncMock()
        efeito = {"side_effect": erro} if erro else {"return_value": status}
        with patch.object(bot.finance_gateway, "poll_link", **efeito):
            asyncio.run(bot.verificar_vinculo_financeiro(context))
        return context

    def test_aprovado_avisa_e_para_de_perguntar(self):
        context = self._consulta("approved")

        context.job.schedule_removal.assert_called_once()
        self.assertIn("Financeiro conectado", context.bot.send_message.call_args.args[1])

    def test_pendente_continua_perguntando_em_silencio(self):
        context = self._consulta("pending")

        context.job.schedule_removal.assert_not_called()
        context.bot.send_message.assert_not_called()

    def test_vencido_avisa_para_pedir_outro(self):
        context = self._consulta("expired")

        context.job.schedule_removal.assert_called_once()
        self.assertIn("/conectar_financeiro", context.bot.send_message.call_args.args[1])

    def test_falha_de_rede_tenta_de_novo_na_proxima_volta(self):
        context = self._consulta(erro=FinanceGatewayError("fora do ar"))

        context.job.schedule_removal.assert_not_called()
        context.bot.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
