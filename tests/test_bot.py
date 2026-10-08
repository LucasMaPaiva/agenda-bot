import asyncio
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
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
from agenda_bot.personal_reminders import apply_update, parse_request  # noqa: E402


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

    def test_consulta_reconhece_periodo_do_dia(self):
        self.assertEqual(bot.periodo_do_texto("o que tenho à noite?"), "noite")
        self.assertEqual(bot.periodo_do_texto("minha agenda de manhã"), "manhã")
        self.assertEqual(bot.periodo_do_texto("compromissos da tarde"), "tarde")
        self.assertEqual(bot.periodo_do_texto("e de madrugada?"), "madrugada")
        self.assertIsNone(bot.periodo_do_texto("o que tenho hoje?"))

    def test_resposta_de_consulta_mostra_somente_o_periodo_pedido(self):
        data_consulta = date(2026, 10, 8)

        def evento(titulo, inicio, fim):
            return {
                "summary": titulo,
                "description": "Responsável: Matheus",
                "start": {
                    "dateTime": datetime.combine(
                        data_consulta, datetime.strptime(inicio, "%H:%M").time(), bot.TZ
                    ).isoformat()
                },
                "end": {
                    "dateTime": datetime.combine(
                        data_consulta, datetime.strptime(fim, "%H:%M").time(), bot.TZ
                    ).isoformat()
                },
            }

        eventos = [
            evento("Café", "08:00", "08:30"),
            evento("Trabalho", "14:00", "17:00"),
            evento("Academia", "19:00", "20:00"),
        ]

        async def imediato(funcao, *args, **kwargs):
            return funcao(*args, **kwargs)

        async def executar():
            mensagem = MensagemFake()
            update = SimpleNamespace(message=mensagem)
            inicio = datetime.combine(data_consulta, datetime.min.time(), bot.TZ)
            with (
                patch.object(bot.asyncio, "to_thread", side_effect=imediato),
                patch.object(bot, "buscar_eventos", return_value=eventos),
            ):
                await bot.responder_consulta(
                    update,
                    (inicio, inicio + timedelta(days=1), "hoje"),
                    "Matheus",
                    periodo="noite",
                )
            return mensagem.respostas[0][0]

        resposta = asyncio.run(executar())
        self.assertIn("hoje à noite", resposta)
        self.assertIn("Academia", resposta)
        self.assertNotIn("Café", resposta)
        self.assertNotIn("Trabalho", resposta)

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

    def test_continuacao_reaproveita_consulta_anterior(self):
        hoje = datetime.now(bot.TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        estado = {
            "tipo": "consulta_agenda",
            "inicio": hoje.isoformat(),
            "fim": (hoje + timedelta(days=1)).isoformat(),
            "rotulo": "hoje",
            "pessoal": True,
            "termo": None,
            "periodo": None,
        }
        with patch.object(bot, "obter_estado_conversa", return_value=estado):
            noite = bot.resolver_continuacao_agenda("e à noite?", "Matheus")
            amanha = bot.resolver_continuacao_agenda("e amanhã?", "Matheus")
            consulta_completa = bot.resolver_continuacao_agenda(
                "minha agenda amanhã", "Matheus"
            )

        self.assertEqual(noite["periodo"], "noite")
        self.assertEqual(noite["intervalo"][0], hoje)
        self.assertTrue(noite["pessoal"])
        self.assertEqual(amanha["intervalo"][0].date(), hoje.date() + timedelta(days=1))
        self.assertIsNone(consulta_completa)

    def test_referencia_contextual_identifica_evento_unico(self):
        evento = {
            "id": "academia-1",
            "titulo": "Academia",
            "inicio": "2026-10-08T19:00:00-04:00",
            "responsavel": "Matheus",
        }
        with patch.object(
            bot, "obter_estado_conversa", return_value={"ultimo_evento": evento}
        ):
            referencia = bot.evento_referenciado(
                "muda esse compromisso para 20h", "Matheus"
            )
        self.assertEqual(referencia, evento)

    def test_edicao_contextual_usa_titulo_e_data_do_evento_referenciado(self):
        dados = {
            "termo": None,
            "data": None,
            "novo_titulo": None,
            "nova_data": None,
            "nova_hora_inicio": "20:00",
            "nova_hora_fim": None,
            "novo_responsavel": None,
        }
        referencia = {
            "titulo": "Academia",
            "inicio": "2026-10-08T19:00:00-04:00",
        }
        with patch.object(
            bot, "gerar_json_operacao", return_value=json.dumps(dados)
        ):
            termo, inicio, fim, alteracoes = bot.extrair_edicao(
                "muda esse compromisso para 20h", referencia
            )
        self.assertEqual(termo, "Academia")
        self.assertEqual(inicio.date(), date(2026, 10, 8))
        self.assertEqual(fim.date(), date(2026, 10, 9))
        self.assertEqual(alteracoes["nova_hora_inicio"], "20:00")

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
            "description": "Responsável: Ambos\nTarefa Google: tarefa-1",
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
        self.assertEqual(novo["task_id"], "tarefa-1")

    def test_horarios_livres_respeitam_eventos_buffer_e_duracao(self):
        data_consulta = datetime.now(bot.TZ).date() + timedelta(days=1)
        eventos = []
        for titulo, inicio, fim in (
            ("Trabalho", "09:00", "10:00"),
            ("Almoço", "12:00", "13:00"),
        ):
            comeco = datetime.fromisoformat(
                f"{data_consulta.isoformat()}T{inicio}:00-04:00"
            )
            termino = datetime.fromisoformat(
                f"{data_consulta.isoformat()}T{fim}:00-04:00"
            )
            eventos.append(
                {
                    "summary": titulo,
                    "description": "Responsável: Matheus",
                    "start": {"dateTime": comeco.isoformat()},
                    "end": {"dateTime": termino.isoformat()},
                }
            )

        configuracoes = {
            "inicio_dia": "08:00",
            "fim_dia": "18:00",
            "buffer_minutos": "15",
        }
        with (
            patch.object(bot, "buscar_eventos", return_value=eventos),
            patch.object(
                bot,
                "obter_configuracao",
                side_effect=lambda role, chave, padrao: configuracoes.get(chave, padrao),
            ),
        ):
            livres = bot.calcular_horarios_livres("Matheus", data_consulta, 60)

        self.assertEqual(
            [(inicio.strftime("%H:%M"), fim.strftime("%H:%M")) for inicio, fim in livres],
            [("10:15", "11:45"), ("13:15", "18:00")],
        )

    def test_pedido_informal_de_horario_livre(self):
        data_consulta, duracao = bot.pedido_horarios_livres(
            "Onde cabe uma reunião de 1h30 amanhã?"
        )
        self.assertEqual(data_consulta, datetime.now(bot.TZ).date() + timedelta(days=1))
        self.assertEqual(duracao, 90)

    def test_pedido_informal_de_planejamento(self):
        amanha = datetime.now(bot.TZ).date() + timedelta(days=1)
        self.assertEqual(
            bot.pedido_planejamento_diario("organiza meu dia amanhã"), amanha
        )
        self.assertEqual(
            bot.pedido_planejamento_diario("planeje meu dia"),
            datetime.now(bot.TZ).date(),
        )
        self.assertIsNone(bot.pedido_planejamento_diario("o que tenho amanhã?"))

    def test_planejamento_prioriza_pendencias_e_nao_duplica_tarefa(self):
        data_consulta = datetime.now(bot.TZ).date() + timedelta(days=1)
        inicio_evento = datetime.combine(
            data_consulta, datetime.strptime("09:00", "%H:%M").time(), bot.TZ
        )
        eventos = [
            {
                "summary": "Reunião",
                "description": "Responsável: Matheus",
                "start": {"dateTime": inicio_evento.isoformat()},
                "end": {
                    "dateTime": (inicio_evento + timedelta(hours=1)).isoformat()
                },
            },
            {
                "summary": "Foco: tarefa já planejada",
                "description": "Responsável: Matheus\nTarefa Google: repetida",
                "start": {
                    "dateTime": (inicio_evento + timedelta(hours=2)).isoformat()
                },
                "end": {
                    "dateTime": (
                        inicio_evento + timedelta(hours=2, minutes=30)
                    ).isoformat()
                },
            },
        ]
        tarefas = [
            {
                "id": "atrasada",
                "title": "Pagar conta",
                "due": f"{(data_consulta - timedelta(days=1)).isoformat()}T00:00:00.000Z",
            },
            {"id": "sem-prazo", "title": "Comprar filtro"},
            {"id": "repetida", "title": "tarefa já planejada"},
            {
                "id": "futura",
                "title": "Resolver depois",
                "due": f"{(data_consulta + timedelta(days=1)).isoformat()}T00:00:00.000Z",
            },
        ]
        configuracoes = {
            "inicio_dia": "08:00",
            "fim_dia": "13:00",
            "buffer_minutos": "15",
            "duracao_tarefa": "30",
        }
        with (
            patch.object(bot, "buscar_eventos", return_value=eventos) as calendario,
            patch.object(bot, "listar_tarefas_google", return_value=tarefas),
            patch.object(
                bot,
                "obter_configuracao",
                side_effect=lambda role, chave, padrao: configuracoes.get(chave, padrao),
            ),
            patch.object(bot, "gerar_resposta_ollama") as ollama,
            patch.object(bot, "gerar_resposta_gemini") as gemini,
        ):
            plano = bot.montar_planejamento_diario("Matheus", data_consulta)

        self.assertEqual(calendario.call_count, 1)
        self.assertEqual(
            [bloco["task_id"] for bloco in plano["blocos"]],
            ["atrasada", "sem-prazo"],
        )
        self.assertEqual(plano["blocos"][0]["inicio"].strftime("%H:%M"), "08:00")
        self.assertNotIn("Resolver depois", plano["texto"])
        self.assertNotIn(
            "Foco: tarefa já planejada",
            [bloco["titulo"] for bloco in plano["blocos"]],
        )
        ollama.assert_not_called()
        gemini.assert_not_called()

    def test_resumo_proativo_e_deterministico(self):
        agora = datetime(2026, 10, 7, 20, 30, tzinfo=bot.TZ)
        amanha = agora.date() + timedelta(days=1)
        inicio = datetime.combine(
            amanha, datetime.strptime("19:00", "%H:%M").time(), bot.TZ
        )
        eventos = [
            {
                "summary": "Academia",
                "description": "Responsável: Matheus",
                "start": {"dateTime": inicio.isoformat()},
                "end": {"dateTime": (inicio + timedelta(hours=1)).isoformat()},
            }
        ]
        with (
            patch.object(bot, "listar_tarefas_google", return_value=[]),
            patch.object(bot, "gerar_resposta_ollama") as ollama,
            patch.object(bot, "gerar_resposta_gemini") as gemini,
        ):
            resumo = bot.montar_resumo_proativo(
                "Matheus", "noite", agora, eventos
            )
        self.assertIn("Prévia de amanhã", resumo)
        self.assertIn("19:00 — Academia", resumo)
        ollama.assert_not_called()
        gemini.assert_not_called()

    def test_lembrete_inteligente_classifica_sem_ia(self):
        self.assertEqual(
            bot.reminder_kind(
                {"summary": "Pagar boleto", "location": "Banco Central"}
            ),
            "deslocamento",
        )
        self.assertEqual(
            bot.reminder_kind({"summary": "Consulta médica"}), "preparo"
        )
        self.assertEqual(bot.reminder_kind({"summary": "Pagar boleto"}), "normal")

    def test_conflito_entre_novo_evento_e_agenda_existente(self):
        inicio = datetime(2026, 10, 7, 18, 0, tzinfo=bot.TZ)
        novo = {
            "titulo": "Academia",
            "inicio": inicio,
            "fim": inicio + timedelta(minutes=75),
        }
        existente = {
            "id": "reuniao-1",
            "summary": "Reunião",
            "start": {"dateTime": (inicio + timedelta(hours=1)).isoformat()},
            "end": {"dateTime": (inicio + timedelta(hours=2)).isoformat()},
        }
        with patch.object(bot, "buscar_eventos", return_value=[existente]):
            conflitos = bot.conflitos_da_replicacao([novo])
        self.assertEqual(len(conflitos), 1)
        self.assertIn("Academia conflita com Reunião", conflitos[0])

    def test_desfazer_criacao_remove_os_eventos_registrados(self):
        eventos_api = MagicMock()
        eventos_api.delete.return_value.execute.return_value = None
        servico = MagicMock()
        servico.events.return_value = eventos_api
        acao = {
            "tipo": "criacao",
            "payload_json": json.dumps({"event_ids": ["evento-1", "evento-2"]}),
        }
        with patch.object(bot, "obter_calendar_service", return_value=servico):
            resultado = bot.executar_desfazer(acao)
        self.assertEqual(eventos_api.delete.call_count, 2)
        self.assertIn("2 evento(s)", resultado)


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.database_path = tempfile.mktemp(suffix=".db")
        self.database_patch = patch.object(bot, "DATABASE_PATH", self.database_path)
        self.database_patch.start()
        bot.inicializar_banco()

    def tearDown(self):
        self.database_patch.stop()
        Path(self.database_path).unlink(missing_ok=True)

    def test_preferencias_rotinas_e_auditoria_persistem(self):
        bot.adicionar_preferencia("Matheus", "Academia é prioridade")
        bot.salvar_configuracao("Matheus", "buffer_minutos", "20")
        bot.salvar_rotina(
            "Matheus",
            "quinta normal",
            [{"titulo": "Academia", "hora": "18:00", "duracao": 75}],
        )
        bot.registrar_acao("Matheus", "criacao", {"event_ids": ["abc"]})

        self.assertEqual(
            bot.listar_preferencias("Matheus")[0]["texto"],
            "Academia é prioridade",
        )
        self.assertEqual(
            bot.obter_configuracao("Matheus", "buffer_minutos", "15"), "20"
        )
        self.assertEqual(bot.obter_rotina("Matheus", "quinta normal")[0]["duracao"], 75)
        self.assertEqual(bot.ultima_acao_pendente("Matheus")["tipo"], "criacao")

    def test_historico_e_estado_da_conversa_persistem_por_usuario(self):
        estado = {
            "tipo": "consulta_agenda",
            "inicio": "2026-10-08T00:00:00-04:00",
            "fim": "2026-10-09T00:00:00-04:00",
            "rotulo": "amanhã",
            "pessoal": True,
            "termo": None,
            "periodo": "noite",
        }
        bot.salvar_estado_conversa("Matheus", estado)
        bot.salvar_turno_conversa(
            "Matheus", "o que tenho amanhã?", "Você tem academia às 19h."
        )

        self.assertEqual(bot.obter_estado_conversa("Matheus"), estado)
        self.assertEqual(
            [item["role"] for item in bot.obter_historico_conversa("Matheus")],
            ["user", "assistant"],
        )
        self.assertEqual(bot.obter_historico_conversa("Letícia"), [])
        self.assertEqual(bot.obter_estado_conversa("Letícia"), {})

        bot.apagar_conversa("Matheus")
        self.assertEqual(bot.obter_historico_conversa("Matheus"), [])
        self.assertEqual(bot.obter_estado_conversa("Matheus"), {})

    def test_resumos_da_manha_e_noite_sao_registrados_separadamente(self):
        dia = date(2026, 10, 7)
        self.assertFalse(bot.resumo_proativo_enviado("Matheus", dia, "manha"))
        bot.marcar_resumo_proativo_enviado("Matheus", dia, "manha")
        self.assertTrue(bot.resumo_proativo_enviado("Matheus", dia, "manha"))
        self.assertFalse(bot.resumo_proativo_enviado("Matheus", dia, "noite"))

    def test_job_de_resumo_nao_consulta_google_fora_do_horario(self):
        with bot.conectar_banco() as conexao:
            conexao.execute(
                """
                INSERT INTO usuarios (
                    role, telegram_user_id, chat_id, registered_at
                ) VALUES (?, ?, ?, ?)
                """,
                ("Matheus", 1, 2, datetime.now(bot.TZ).isoformat()),
            )
        contexto = SimpleNamespace(bot=MagicMock())
        with (
            patch.object(bot, "buscar_eventos") as calendario,
            patch.object(bot, "listar_tarefas_google") as tarefas,
        ):
            asyncio.run(bot.verificar_resumos_diarios(contexto))
        calendario.assert_not_called()
        tarefas.assert_not_called()

    def test_lembrete_inteligente_respeita_antecedencia_e_nao_duplica(self):
        class DataHoraFixa(datetime):
            @classmethod
            def now(cls, timezone=None):
                return cls(2026, 10, 7, 8, 0, tzinfo=timezone or bot.TZ)

        with bot.conectar_banco() as conexao:
            conexao.execute(
                """
                INSERT INTO usuarios (
                    role, telegram_user_id, chat_id, registered_at
                ) VALUES (?, ?, ?, ?)
                """,
                ("Matheus", 1, 2, datetime.now(bot.TZ).isoformat()),
            )
        bot.salvar_configuracao("Matheus", "lembrete_preparo_minutos", "60")
        inicio = datetime(2026, 10, 7, 8, 50, tzinfo=bot.TZ)
        evento = {
            "id": "dentista-1",
            "summary": "Dentista",
            "description": "Responsável: Matheus",
            "start": {"dateTime": inicio.isoformat()},
            "end": {"dateTime": (inicio + timedelta(hours=1)).isoformat()},
        }
        telegram = SimpleNamespace(send_message=AsyncMock())
        contexto = SimpleNamespace(
            bot=telegram,
            application=SimpleNamespace(bot_data={}),
        )
        with (
            patch.object(bot, "datetime", DataHoraFixa),
            patch.object(bot, "buscar_eventos", return_value=[evento]) as calendario,
        ):
            asyncio.run(bot.verificar_lembretes(contexto))
            asyncio.run(bot.verificar_lembretes(contexto))

        self.assertEqual(telegram.send_message.await_count, 1)
        mensagem = telegram.send_message.await_args.kwargs["text"]
        self.assertIn("Hora de se preparar", mensagem)
        self.assertIn("começa em 50 min", mensagem)
        self.assertEqual(
            calendario.call_args_list[0].args[1] - calendario.call_args_list[0].args[0],
            timedelta(minutes=60),
        )
        with bot.conectar_banco() as conexao:
            registro = conexao.execute(
                "SELECT reminder_minutes FROM notificacoes WHERE event_id = ?",
                ("dentista-1",),
            ).fetchone()
        self.assertEqual(registro["reminder_minutes"], 60)

    def test_lembretes_desligados_nao_consultam_google(self):
        with bot.conectar_banco() as conexao:
            conexao.execute(
                """
                INSERT INTO usuarios (
                    role, telegram_user_id, chat_id, registered_at
                ) VALUES (?, ?, ?, ?)
                """,
                ("Letícia", 3, 4, datetime.now(bot.TZ).isoformat()),
            )
        bot.salvar_configuracao("Letícia", "lembretes_ativos", "nao")
        contexto = SimpleNamespace(
            bot=SimpleNamespace(send_message=AsyncMock()),
            application=SimpleNamespace(bot_data={}),
        )
        with patch.object(bot, "buscar_eventos") as calendario:
            asyncio.run(bot.verificar_lembretes(contexto))
        calendario.assert_not_called()

    def test_lembrete_avulso_persiste_reivindica_e_remove_apos_envio(self):
        agora = datetime.now(bot.TZ).replace(second=0, microsecond=0)
        lembrete_id = bot.salvar_lembrete_avulso(
            "Matheus", 2, "• comprar ração\n• encher pneu", agora
        )
        vencidos = bot.reivindicar_lembretes_avulsos_vencidos(agora)
        self.assertEqual([row["id"] for row in vencidos], [lembrete_id])
        self.assertEqual(vencidos[0]["texto"], "• comprar ração\n• encher pneu")
        self.assertEqual(bot.reivindicar_lembretes_avulsos_vencidos(agora), [])

        bot.liberar_lembrete_avulso(lembrete_id)
        contexto = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        asyncio.run(bot.verificar_lembretes_avulsos(contexto))
        contexto.bot.send_message.assert_awaited_once()
        with bot.conectar_banco() as conexao:
            restante = conexao.execute(
                "SELECT id FROM lembretes_avulsos WHERE id = ?", (lembrete_id,)
            ).fetchone()
        self.assertIsNone(restante)


class PersonalReminderLogicTest(unittest.TestCase):
    def test_parseia_um_lembrete_com_varios_itens(self):
        agora = datetime(2026, 10, 8, 10, 0, tzinfo=bot.TZ)
        pedido = parse_request(
            "preciso que me lembre as 12 de passar no mercado e comprar ração, "
            "encher o pneu e apertar a corrente da moto",
            agora,
        )
        self.assertEqual(
            pedido["items"],
            [
                "passar no mercado",
                "comprar ração",
                "encher o pneu",
                "apertar a corrente da moto",
            ],
        )
        self.assertEqual(pedido["time"].strftime("%H:%M"), "12:00")
        self.assertTrue(bot.eh_pedido_lembrete_avulso("me lembra às 12 de comprar ração"))

    def test_edicao_curta_muda_horario_e_remove_item(self):
        agora = datetime(2026, 10, 8, 10, 0, tzinfo=bot.TZ)
        draft = parse_request(
            "me lembra hoje às 12 de comprar ração e encher o pneu", agora
        )
        revised = apply_update(draft, "troca para 13h", agora)
        revised = apply_update(revised, "tira encher o pneu", agora)
        self.assertEqual(revised["time"].strftime("%H:%M"), "13:00")
        self.assertEqual(revised["items"], ["comprar ração"])


class PersonalReminderFlowTest(unittest.TestCase):
    def setUp(self):
        self.database_path = tempfile.mktemp(suffix=".db")
        self.database_patch = patch.object(bot, "DATABASE_PATH", self.database_path)
        self.database_patch.start()
        bot.inicializar_banco()

    def tearDown(self):
        self.database_patch.stop()
        Path(self.database_path).unlink(missing_ok=True)

    def test_lembrete_so_e_salvo_apos_confirmacao(self):
        # O fluxo de conversa cria somente uma prévia; a ação do botão persiste.
        async def fluxo():
            contexto = SimpleNamespace(user_data={})
            mensagem = MensagemFake()
            update = SimpleNamespace(
                message=mensagem,
                effective_user=SimpleNamespace(id=1),
                effective_chat=SimpleNamespace(id=42),
            )
            with patch.object(bot, "role_do_usuario", return_value="Matheus"):
                await bot.handle_message(
                    SimpleNamespace(
                        message=SimpleNamespace(
                            text="me lembra amanhã às 12 de comprar ração e encher pneu",
                            reply_text=mensagem.reply_text,
                        ),
                        effective_user=update.effective_user,
                        effective_chat=update.effective_chat,
                    ),
                    contexto,
                )
                with bot.conectar_banco() as conexao:
                    antes = conexao.execute(
                        "SELECT COUNT(*) AS total FROM lembretes_avulsos"
                    ).fetchone()["total"]
                self.assertEqual(antes, 0)
                token = next(iter(contexto.user_data["rascunhos_lembretes_avulsos"]))
                query = QueryFake(f"prc:{token}")
                await bot.callback_lembrete_avulso(
                    SimpleNamespace(callback_query=query), contexto
                )
                with bot.conectar_banco() as conexao:
                    depois = conexao.execute(
                        "SELECT texto FROM lembretes_avulsos"
                    ).fetchone()["texto"]
                self.assertEqual(depois, "• comprar ração\n• encher pneu")
                self.assertIn("amanhã às 12:00", query.edicoes[0][0])

        asyncio.run(fluxo())

    def test_horario_faltando_e_perguntado_antes_da_confirmacao(self):
        async def fluxo():
            context = SimpleNamespace(user_data={})
            message = MensagemFake()
            update = SimpleNamespace(
                message=message,
                effective_chat=SimpleNamespace(id=42),
            )
            await bot.personal_reminder_handlers.begin(
                bot, update, context, "Matheus", "me lembra de comprar ração"
            )
            self.assertIn("dia e horário", message.respostas[-1][0])
            self.assertNotIn("rascunhos_lembretes_avulsos", context.user_data)
            await bot.personal_reminder_handlers.continue_input(
                bot, update, context, "Matheus", "amanhã às 12"
            )
            self.assertIn("amanhã às 12:00", message.respostas[-1][0])
            self.assertIn("rascunhos_lembretes_avulsos", context.user_data)

        asyncio.run(fluxo())

    def test_configuracao_de_lembrete_e_individual(self):
        async def executar():
            mensagem = MensagemFake()
            contexto = SimpleNamespace(args=["preparo", "75"])
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=1), message=mensagem
            )
            with patch.object(bot, "role_do_usuario", return_value="Letícia"):
                await bot.lembretes_cmd(update, contexto)
            return mensagem

        mensagem = asyncio.run(executar())
        self.assertEqual(bot.configuracao_lembretes("Letícia")["preparo"], 75)
        self.assertEqual(bot.configuracao_lembretes("Matheus")["preparo"], 60)
        self.assertIn("75 minutos", mensagem.respostas[0][0])


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
                patch.object(bot, "conflitos_da_replicacao", return_value=[]),
                patch.object(bot, "role_do_usuario", return_value="Matheus"),
                patch.object(
                    bot,
                    "criar_eventos_calendar_com_ids",
                    side_effect=lambda eventos: criados.extend(eventos) or ["evento-1"],
                ),
                patch.object(bot, "registrar_acao"),
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

    def test_planejamento_so_cria_blocos_depois_da_confirmacao(self):
        async def imediato(funcao, *args, **kwargs):
            return funcao(*args, **kwargs)

        inicio = datetime(2026, 10, 8, 8, 0, tzinfo=bot.TZ)
        bloco = {
            "titulo": "Foco: Pagar conta",
            "responsavel": "Matheus",
            "inicio": inicio,
            "fim": inicio + timedelta(minutes=30),
            "recorrencia": "nenhuma",
            "task_id": "tarefa-1",
        }
        criados = []
        contexto = SimpleNamespace(user_data={})
        mensagem = MensagemFake()

        async def executar():
            with (
                patch.object(bot.asyncio, "to_thread", side_effect=imediato),
                patch.object(
                    bot,
                    "montar_planejamento_diario",
                    return_value={"texto": "Plano", "blocos": [bloco]},
                ),
                patch.object(bot, "role_do_usuario", return_value="Matheus"),
                patch.object(
                    bot,
                    "criar_eventos_calendar_com_ids",
                    side_effect=lambda eventos: criados.extend(eventos) or ["evento-1"],
                ),
                patch.object(bot, "registrar_acao"),
            ):
                await bot.enviar_planejamento_diario(
                    SimpleNamespace(message=mensagem), contexto, "Matheus", inicio.date()
                )
                self.assertFalse(criados)
                token = next(iter(contexto.user_data["criacoes_pendentes"]))
                query = QueryFake(f"gc:{token}")
                await bot.callback_criacao(
                    SimpleNamespace(callback_query=query), contexto
                )
                self.assertEqual(criados, [bloco])

        asyncio.run(executar())


if __name__ == "__main__":
    unittest.main()
