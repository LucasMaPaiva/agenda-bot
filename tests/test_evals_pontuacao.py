import unittest

from evals.pontuacao import avaliar_caso, campo_correto


class PontuacaoTest(unittest.TestCase):
    def test_titulo_aceita_palavra_chave_sem_acento(self):
        self.assertTrue(campo_correto("titulo", ["médico"], "Consulta no Medico"))
        self.assertFalse(campo_correto("titulo", ["dentista"], "Consulta"))

    def test_hora_com_e_sem_zero_a_esquerda(self):
        self.assertTrue(campo_correto("hora_inicio", "09:00", "9:00"))
        self.assertFalse(campo_correto("hora_inicio", "09:00", "21:00"))

    def test_dias_semana_ignora_ordem(self):
        self.assertTrue(campo_correto("dias_semana", ["segunda", "quarta"], ["quarta", "segunda"]))

    def test_campos_ignorados_nao_entram_na_nota(self):
        caso = {
            "esperado": {"titulo": ["feira"], "recorrencia": "semanal"},
            "ignorar": ["data"],
        }
        notas = avaliar_caso(caso, {"titulo": "Feira", "recorrencia": "semanal", "data": "x"})
        self.assertEqual(notas, {"titulo": True, "recorrencia": True})

    def test_sem_saida_do_modelo_erra_tudo(self):
        caso = {"esperado": {"titulo": ["x"], "data": "2026-11-13"}}
        self.assertEqual(avaliar_caso(caso, None), {"titulo": False, "data": False})


if __name__ == "__main__":
    unittest.main()
