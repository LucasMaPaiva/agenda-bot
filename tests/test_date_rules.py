import unittest
from datetime import date

from agenda_bot.date_rules import (
    corrigir_extracao,
    datas_no_texto,
    horarios_no_texto,
    texto_tem_recorrencia,
)

QUINTA = date(2026, 11, 12)


class DatasTest(unittest.TestCase):
    def d(self, texto):
        return [x.isoformat() for x in datas_no_texto(texto, QUINTA)]

    def test_relativas(self):
        self.assertEqual(self.d("academia hoje"), ["2026-11-12"])
        self.assertEqual(self.d("dentista amanhã"), ["2026-11-13"])
        self.assertEqual(self.d("encomenda depois de amanhã"), ["2026-11-14"])

    def test_dias_da_semana(self):
        self.assertEqual(self.d("conta de luz sexta"), ["2026-11-13"])
        self.assertEqual(self.d("cinema terça que vem"), ["2026-11-17"])
        self.assertEqual(self.d("revisão quinta da semana que vem"), ["2026-11-19"])
        self.assertEqual(self.d("reunião quinta"), ["2026-11-19"])

    def test_datas_absolutas(self):
        self.assertEqual(self.d("aniversário 05/12"), ["2026-12-05"])
        self.assertEqual(self.d("boleto dia 25"), ["2026-11-25"])
        self.assertEqual(self.d("vacina dia 3"), ["2026-12-03"])
        self.assertEqual(self.d("oftalmo dia 1º de dezembro"), ["2026-12-01"])
        self.assertEqual(self.d("reunião dia 27/11"), ["2026-11-27"])

    def test_intervalo(self):
        self.assertEqual(self.d("viagem de 20/11 até 23/11"), ["2026-11-20", "2026-11-23"])


class HorariosTest(unittest.TestCase):
    def test_formatos(self):
        self.assertEqual(horarios_no_texto("às 9h"), ["09:00"])
        self.assertEqual(horarios_no_texto("dia 20 às 14h30"), ["14:30"])
        self.assertEqual(horarios_no_texto("lavar o carro às 9"), ["09:00"])
        self.assertEqual(horarios_no_texto("culto às 7 da noite"), ["19:00"])
        self.assertEqual(horarios_no_texto("escola às 5 da tarde"), ["17:00"])
        self.assertEqual(horarios_no_texto("aniversário ao meio-dia"), ["12:00"])
        self.assertEqual(horarios_no_texto("voo 10/12 às 5h40"), ["05:40"])

    def test_inicio_e_fim(self):
        self.assertEqual(horarios_no_texto("das 14h às 17h"), ["14:00", "17:00"])
        self.assertEqual(horarios_no_texto("quarta 12h30 às 14h"), ["12:30", "14:00"])


class RecorrenciaTest(unittest.TestCase):
    def test_detecta(self):
        self.assertTrue(texto_tem_recorrencia("academia toda segunda e quarta"))
        self.assertTrue(texto_tem_recorrencia("aula de segunda a quinta"))
        self.assertTrue(texto_tem_recorrencia("natação terça e quinta"))
        self.assertFalse(texto_tem_recorrencia("Matheus e Letícia: casamento sábado"))
        self.assertFalse(texto_tem_recorrencia("plantão de 14/11 a 16/11"))


class CorrigirExtracaoTest(unittest.TestCase):
    def test_corrige_data_trocada_e_recorrencia_inventada(self):
        modelo = {
            "titulo": "Dentista", "data": "2026-11-12", "data_fim": "2026-11-13",
            "hora_inicio": "09:00", "hora_fim": None, "responsavel": "Letícia",
            "recorrencia": "diaria", "dias_semana": [], "ordem_mes": None,
        }
        dados = corrigir_extracao(modelo, "Dentista amanhã às 9h", QUINTA)
        self.assertEqual(dados["data"], "2026-11-13")
        self.assertIsNone(dados["data_fim"])
        self.assertEqual(dados["recorrencia"], "nenhuma")

    def test_recorrencia_mensal(self):
        modelo = {"recorrencia": "nenhuma", "dias_semana": ["segunda"], "ordem_mes": None,
                  "hora_inicio": "10:00", "hora_fim": None}
        dados = corrigir_extracao(modelo, "condomínio todo primeiro domingo do mês às 10h", QUINTA)
        self.assertEqual(
            (dados["recorrencia"], dados["dias_semana"], dados["ordem_mes"]),
            ("mensal", ["domingo"], 1),
        )

    def test_recorrencia_sem_ate_ignora_fim_do_modelo(self):
        modelo = {"data": "2026-11-12", "data_fim": "2026-11-12", "recorrencia": "nenhuma",
                  "dias_semana": [], "ordem_mes": None, "hora_inicio": "09:00", "hora_fim": None}
        dados = corrigir_extracao(modelo, "missa todo último domingo do mês às 9h", QUINTA)
        self.assertEqual((dados["recorrencia"], dados["ordem_mes"]), ("mensal", -1))
        self.assertIsNone(dados["data_fim"])

    def test_sem_data_no_texto_mantem_o_modelo(self):
        modelo = {"data": "2026-11-20", "data_fim": None, "recorrencia": "semanal",
                  "dias_semana": ["sexta"], "hora_inicio": "8:00", "hora_fim": None}
        dados = corrigir_extracao(modelo, "teste", QUINTA)
        self.assertEqual(dados["recorrencia"], "semanal")
        self.assertEqual(dados["hora_inicio"], "08:00")


if __name__ == "__main__":
    unittest.main()
