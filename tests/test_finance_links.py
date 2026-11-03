import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from agenda_bot.finance_gateway import FinanceGateway, FinanceGatewayError
from agenda_bot.finance_tokens import FinanceTokenStore


def resposta(status_code, payload):
    response = MagicMock(status_code=status_code)
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class FinanceLinkTest(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = FinanceTokenStore(self.db_path)
        self.gateway = FinanceGateway(
            "https://financeiro.example.com",
            {"Matheus": "token-do-env"},
            token_store=self.store,
            link_secret="segredo",
        )

    def tearDown(self):
        os.remove(self.db_path)

    def test_token_vinculado_vence_o_do_env(self):
        self.assertEqual(self.gateway.token_for("Matheus"), "token-do-env")
        self.store.save("Matheus", "token-vinculado")
        self.store.save("Matheus", "token-novo")
        self.assertEqual(self.gateway.token_for("Matheus"), "token-novo")
        self.assertTrue(self.gateway.configured("Matheus"))
        self.assertFalse(self.gateway.configured("Letícia"))

    def test_pede_o_vinculo_com_o_segredo(self):
        payload = {
            "data": {
                "code": "abc",
                "url": "https://financeiro.example.com/conectar/telegram/abc",
                "verification_code": "123 456",
            }
        }
        with patch(
            "agenda_bot.finance_gateway.requests.request",
            return_value=resposta(201, payload),
        ) as request:
            vinculo = self.gateway.start_link()

        self.assertEqual(vinculo.verification_code, "123 456")
        self.assertEqual(
            request.call_args.kwargs["headers"]["X-Telegram-Link-Secret"], "segredo"
        )
        self.assertNotIn("Authorization", request.call_args.kwargs["headers"])

    def test_guarda_o_token_quando_aprovado(self):
        with patch(
            "agenda_bot.finance_gateway.requests.request",
            side_effect=[
                resposta(202, {"data": {"status": "pending"}}),
                resposta(200, {"data": {"status": "approved", "token": "7|novo"}}),
            ],
        ):
            self.assertEqual(self.gateway.poll_link("Letícia", "abc"), "pending")
            self.assertEqual(self.gateway.poll_link("Letícia", "abc"), "approved")

        self.assertEqual(self.gateway.token_for("Letícia"), "7|novo")

    def test_link_desconhecido_ou_vencido_para_de_perguntar(self):
        with patch(
            "agenda_bot.finance_gateway.requests.request",
            side_effect=[
                resposta(404, {}),
                resposta(410, {"data": {"status": "expired"}}),
            ],
        ):
            self.assertEqual(self.gateway.poll_link("Matheus", "x"), "expired")
            self.assertEqual(self.gateway.poll_link("Matheus", "x"), "expired")

    def test_sem_segredo_o_vinculo_fica_desligado(self):
        gateway = FinanceGateway("https://financeiro.example.com", {}, token_store=self.store)
        self.assertFalse(gateway.can_link())
        with self.assertRaises(FinanceGatewayError):
            gateway.start_link()

    def test_segredo_recusado_vira_erro_seguro(self):
        with patch(
            "agenda_bot.finance_gateway.requests.request",
            return_value=resposta(401, {}),
        ):
            with self.assertRaises(FinanceGatewayError):
                self.gateway.start_link()


if __name__ == "__main__":
    unittest.main()
