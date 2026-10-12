"""Cliente da API financeira dedicada ao Telegram."""

from typing import Any

import requests


class FinanceGatewayError(RuntimeError):
    """Erro seguro para ser apresentado ao usuário sem expor dados financeiros."""


class FinanceGateway:
    def __init__(
        self,
        base_url: str,
        tokens: dict[str, str],
        timeout_seconds: int = 10,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tokens = tokens
        self.timeout_seconds = timeout_seconds

    def configured(self, role: str) -> bool:
        return bool(self.base_url and self.tokens.get(role))

    def categories(self, role: str) -> list[dict]:
        return self._request(role, "GET", "/api/v1/telegram/categories")["data"]

    def create_expense(self, role: str, expense: dict) -> dict:
        return self._request(
            role,
            "POST",
            "/api/v1/telegram/expenses",
            json=expense,
        )["data"]

    def payables(self, role: str) -> dict:
        return self._request(role, "GET", "/api/v1/telegram/payables")

    def summary(self, role: str, period: str | None = None) -> dict:
        params = {"period": period} if period else None
        return self._request(
            role,
            "GET",
            "/api/v1/telegram/summary",
            params=params,
        )["data"]

    def _request(
        self,
        role: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict:
        token = self.tokens.get(role, "")
        if not self.base_url or not token:
            raise FinanceGatewayError(
                f"A integração financeira de {role} ainda não está configurada."
            )

        try:
            response = requests.request(
                method,
                f"{self.base_url}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=self.timeout_seconds,
                **kwargs,
            )
        except requests.Timeout as error:
            raise FinanceGatewayError(
                "O financeiro demorou demais para responder. Tente novamente."
            ) from error
        except requests.RequestException as error:
            raise FinanceGatewayError(
                "Não consegui acessar o financeiro agora. Tente novamente."
            ) from error

        if response.status_code in {401, 403}:
            raise FinanceGatewayError(
                "A autorização do financeiro expirou ou não tem permissão."
            )
        if response.status_code == 422:
            try:
                errors = response.json().get("errors", {})
                first = next(
                    (message for messages in errors.values() for message in messages),
                    None,
                )
            except (ValueError, AttributeError, TypeError):
                first = None
            raise FinanceGatewayError(first or "Os dados do gasto não são válidos.")
        try:
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            raise FinanceGatewayError(
                "O financeiro devolveu uma resposta inesperada."
            ) from error
        if not isinstance(payload, dict):
            raise FinanceGatewayError("O financeiro devolveu uma resposta inesperada.")
        return payload
