import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch


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


class AgendaLogicTest(unittest.TestCase):
    def resposta(self, dados):
        return SimpleNamespace(text=json.dumps(dados))

    def evento_base(self):
        return {
            "titulo": "Academia",
            "data": "2026-10-07",
            "data_fim": None,
            "hora_inicio": "18:00",
            "hora_fim": "19:15",
            "responsavel": "Ambos",
            "recorrencia": "nenhuma",
            "dias_semana": [],
            "ordem_mes": None,
        }

    def test_consultas_informais(self):
        for texto in (
            "o que tenho pra hoje",
            "temos oq pra hoje",
            "hoje tenho oq",
            "q tem hj",
            "alguma coisa pra hoje",
        ):
            self.assertIsNotNone(bot.intervalo_de_consulta(texto), texto)

        for texto in (
            "academia hoje às 18h",
            "agenda dentista amanhã às 15h",
            "troca de óleo amanhã às 10h",
        ):
            self.assertIsNone(bot.intervalo_de_consulta(texto), texto)

        self.assertIsNone(bot.intervalo_de_consulta("o que tenho dia 15/10"))
        self.assertIsNone(bot.intervalo_de_consulta("o que tenho quinta"))

    def test_consulta_pessoal_ou_conjunta(self):
        self.assertTrue(bot.consulta_eh_pessoal("o que eu tenho hoje"))
        self.assertTrue(bot.consulta_eh_pessoal("minha agenda amanhã"))
        self.assertFalse(bot.consulta_eh_pessoal("o que temos hoje"))
        self.assertFalse(bot.consulta_eh_pessoal("agenda de todos"))

    def test_intervalo_e_recorrencias(self):
        faixa = {**self.evento_base(), "data": "2026-10-10", "data_fim": "2026-10-18"}
        with patch.object(bot, "gerar_resposta_gemini", return_value=self.resposta(faixa)):
            self.assertEqual(len(bot.extrair_eventos("teste")), 9)

        semanal = {
            **self.evento_base(),
            "data": "2026-10-05",
            "data_fim": "2026-10-18",
            "recorrencia": "semanal",
            "dias_semana": ["segunda", "quarta"],
        }
        with patch.object(bot, "gerar_resposta_gemini", return_value=self.resposta(semanal)):
            eventos = bot.extrair_eventos("teste")
        self.assertEqual(
            [evento["inicio"].date().isoformat() for evento in eventos],
            ["2026-10-05", "2026-10-07", "2026-10-12", "2026-10-14"],
        )

        mensal = {
            **self.evento_base(),
            "data": "2026-10-01",
            "data_fim": "2026-12-31",
            "recorrencia": "mensal",
            "dias_semana": ["domingo"],
            "ordem_mes": 1,
        }
        with patch.object(bot, "gerar_resposta_gemini", return_value=self.resposta(mensal)):
            eventos = bot.extrair_eventos("teste")
        self.assertEqual(
            [evento["inicio"].date().isoformat() for evento in eventos],
            ["2026-10-04", "2026-11-01", "2026-12-06"],
        )

    def test_intencoes_de_cancelar_e_editar(self):
        self.assertTrue(bot.eh_pedido_cancelamento("exclui academia amanhã"))
        self.assertTrue(bot.eh_pedido_edicao("muda academia para 18h30"))
        self.assertFalse(bot.eh_pedido_edicao("troca de óleo amanhã às 10h"))

    def test_criacao_explicita_nao_confunde_conversa(self):
        self.assertTrue(
            bot.eh_pedido_criacao_evento("marque reunião amanhã às 19h")
        )
        self.assertFalse(bot.eh_pedido_criacao_evento("coloca isso em perspectiva"))
        self.assertTrue(
            bot.eh_pedido_criacao_tarefa("coloca uma tarefa para comprar sabão")
        )

    def test_consulta_de_horario_filtra_o_compromisso(self):
        self.assertTrue(
            bot.eh_consulta_de_compromisso_especifico(
                "que horas é a academia hoje?"
            )
        )
        eventos = [
            {"summary": "Academia"},
            {"summary": "Reunião Classe Companheiro"},
            {"summary": "Prontos + Deslocamento"},
        ]
        self.assertEqual(
            bot.filtrar_eventos_por_termo(eventos, "academia"), [eventos[0]]
        )
        self.assertEqual(
            bot.filtrar_eventos_por_termo(eventos, "reunião companheiro"),
            [eventos[1]],
        )
        self.assertEqual(
            bot.filtrar_eventos_por_termo(eventos, "protnos + deslocamento"),
            [eventos[2]],
        )

    def test_contexto_de_conversa_inclui_conflito_calculado(self):
        inicio = datetime(2026, 10, 8, 18, 0, tzinfo=bot.TZ)
        eventos = [
            {
                "summary": "Academia",
                "description": "Responsável: Ambos",
                "start": {"dateTime": inicio.isoformat()},
                "end": {"dateTime": (inicio + timedelta(minutes=75)).isoformat()},
            },
            {
                "summary": "Reunião",
                "description": "Responsável: Matheus",
                "start": {"dateTime": (inicio + timedelta(minutes=60)).isoformat()},
                "end": {"dateTime": (inicio + timedelta(minutes=120)).isoformat()},
            },
        ]
        with patch.object(bot, "buscar_eventos", return_value=eventos):
            contexto = bot.contexto_agenda_para_conversa("Matheus")
        self.assertIn("Academia conflita com Reunião por 15 min", contexto)

    def test_operacao_usa_modelo_local_se_gemini_estiver_indisponivel(self):
        dados = self.evento_base()
        with (
            patch.object(bot, "OLLAMA_ENABLED", True),
            patch.object(
                bot, "gerar_resposta_gemini", side_effect=RuntimeError("offline")
            ),
            patch.object(
                bot, "gerar_resposta_ollama", return_value=json.dumps(dados)
            ) as ollama,
        ):
            resposta = bot.gerar_json_operacao("teste", bot.EVENTO_SCHEMA)
        self.assertEqual(json.loads(resposta), dados)
        ollama.assert_called_once()

    def test_extrai_e_prepara_replicacao_preservando_horarios(self):
        dados = {
            "data_origem": "2026-10-06",
            "data_destino": "2026-10-07",
            "termos": ["academia", "trabalho"],
        }
        with patch.object(
            bot, "gerar_json_operacao", return_value=json.dumps(dados)
        ):
            origem, destino, termos = bot.extrair_replicacao("teste")
        self.assertEqual(origem.isoformat(), "2026-10-06")
        self.assertEqual(destino.isoformat(), "2026-10-07")
        self.assertEqual(termos, ["academia", "trabalho"])

        inicio = datetime(2026, 10, 6, 18, 0, tzinfo=bot.TZ)
        evento = {
            "id": "academia-1",
            "summary": "Academia",
            "description": "Responsável: Ambos",
            "start": {"dateTime": inicio.isoformat()},
            "end": {"dateTime": (inicio + timedelta(minutes=75)).isoformat()},
        }
        copia = bot.preparar_eventos_replicados([evento], destino)[0]
        self.assertEqual(copia["inicio"].isoformat(), "2026-10-07T18:00:00-04:00")
        self.assertEqual(copia["fim"].isoformat(), "2026-10-07T19:15:00-04:00")
        self.assertEqual(copia["responsavel"], "Ambos")

    def test_replicacao_reconhece_verbo_e_exige_todos_os_eventos(self):
        self.assertTrue(
            bot.eh_pedido_replicacao(
                "replica acordar, culto e trabalho de terça para quarta"
            )
        )
        eventos = [{"id": "1", "summary": "Academia"}]
        with self.assertRaisesRegex(ValueError, "trabalho"):
            bot.selecionar_eventos_para_replicar(
                eventos, ["academia", "trabalho"]
            )

    def test_preparar_edicao_preserva_duracao(self):
        original = {
            "id": "abc",
            "summary": "Academia",
            "description": "Responsável: Ambos",
            "start": {"dateTime": "2026-10-07T18:00:00-04:00"},
            "end": {"dateTime": "2026-10-07T19:15:00-04:00"},
        }
        alteracoes = {
            "novo_titulo": None,
            "nova_data": None,
            "nova_hora_inicio": "18:30",
            "nova_hora_fim": None,
            "novo_responsavel": "Letícia",
        }
        novo = bot.preparar_evento_editado(original, alteracoes)
        self.assertEqual(novo["inicio"].strftime("%H:%M"), "18:30")
        self.assertEqual(novo["fim"].strftime("%H:%M"), "19:45")
        self.assertEqual(novo["responsavel"], "Letícia")


class MensagemFake:
    def __init__(self):
        self.respostas = []

    async def reply_text(self, texto, **kwargs):
        self.respostas.append((texto, kwargs))


class QueryFake:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.edicoes = []

    async def answer(self, *args, **kwargs):
        return None

    async def edit_message_text(self, texto, **kwargs):
        self.edicoes.append((texto, kwargs))


class ConfirmationFlowTest(unittest.TestCase):
    def test_criacao_so_ocorre_depois_da_confirmacao(self):
        async def imediato(funcao, *args, **kwargs):
            return funcao(*args, **kwargs)

        inicio = datetime(2026, 10, 7, 18, 0, tzinfo=bot.TZ)
        evento = {
            "titulo": "Academia",
            "responsavel": "Ambos",
            "inicio": inicio,
            "fim": inicio + timedelta(minutes=75),
            "recorrencia": "nenhuma",
        }
        criados = []
        contexto = SimpleNamespace(user_data={})
        mensagem = MensagemFake()

        async def executar():
            with (
                patch.object(bot.asyncio, "to_thread", side_effect=imediato),
                patch.object(bot, "extrair_eventos", return_value=[evento]),
                patch.object(bot, "role_do_usuario", return_value="Matheus"),
                patch.object(
                    bot,
                    "criar_eventos_calendar",
                    side_effect=lambda eventos: criados.extend(eventos) or len(eventos),
                ),
            ):
                await bot.iniciar_criacao(
                    SimpleNamespace(message=mensagem), contexto, "academia amanhã"
                )
                self.assertFalse(criados)
                token = next(iter(contexto.user_data["criacoes_pendentes"]))
                query = QueryFake(f"gc:{token}")
                await bot.callback_criacao(
                    SimpleNamespace(callback_query=query), contexto
                )
                self.assertEqual(len(criados), 1)

        asyncio.run(executar())


if __name__ == "__main__":
    unittest.main()
