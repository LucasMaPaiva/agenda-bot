import unittest
from unittest.mock import MagicMock, patch

from agenda_bot.finance_gateway import FinanceGateway, FinanceGatewayError


class FinanceGatewayTest(unittest.TestCase):
    def test_usa_o_mesmo_bearer_token_em_todas_as_operacoes(self):
        gateway = FinanceGateway(
            "https://financeiro.example.com",
            {"Matheus": "token-individual"},
        )
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": []}
        response.raise_for_status.return_value = None

        with patch(
            "agenda_bot.finance_gateway.requests.request", return_value=response
        ) as request:
            gateway.categories("Matheus")
            gateway.payables("Matheus")
            gateway.summary("Matheus")
            gateway.create_expense(
                "Matheus",
                {
                    "description": "Coca-Cola",
                    "amount": "24,50",
                    "date": "2026-10-12",
                    "category_id": 4,
                },
            )

        self.assertEqual(request.call_count, 4)
        for chamada in request.call_args_list:
            self.assertEqual(
                chamada.kwargs["headers"]["Authorization"],
                "Bearer token-individual",
            )

    def test_nao_permita_role_sem_token(self):
        gateway = FinanceGateway(
            "https://financeiro.example.com",
            {"Matheus": "token-individual"},
        )

        with self.assertRaises(FinanceGatewayError):
            gateway.summary("Letícia")


if __name__ == "__main__":
    unittest.main()
