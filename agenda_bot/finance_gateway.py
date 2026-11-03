"""Cliente da API financeira dedicada ao Telegram."""

from dataclasses import dataclass
from typing import Any, Optional

import requests

from agenda_bot.finance_tokens import FinanceTokenStore


class FinanceGatewayError(RuntimeError):
    """Erro seguro para ser apresentado ao usuário sem expor dados financeiros."""


@dataclass(frozen=True)
class FinanceLink:
    """Um pedido de vínculo aberto no financeiro."""

    code: str
    url: str
    verification_code: str


class FinanceGateway:
    def __init__(
        self,
        base_url: str,
        tokens: dict[str, str],
        timeout_seconds: int = 10,
        token_store: Optional[FinanceTokenStore] = None,
        link_secret: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tokens = tokens
        self.timeout_seconds = timeout_seconds
        self.token_store = token_store
        self.link_secret = link_secret

    def token_for(self, role: str) -> str:
        """O token vinculado por link vence o do .env, que fica como reserva."""
        stored = self.token_store.get(role) if self.token_store else ""
        return stored or self.tokens.get(role, "")

    def configured(self, role: str) -> bool:
        return bool(self.base_url and self.token_for(role))

    def can_link(self) -> bool:
        return bool(self.base_url and self.link_secret and self.token_store)

    def start_link(self) -> FinanceLink:
        payload = self._link_request("POST", "/api/v1/telegram/link-requests", {201})
        data = payload.get("data", {})
        try:
            return FinanceLink(
                code=data["code"],
                url=data["url"],
                verification_code=data["verification_code"],
            )
        except (KeyError, TypeError) as error:
            raise FinanceGatewayError(
                "O financeiro devolveu uma resposta inesperada."
            ) from error

    def poll_link(self, role: str, code: str) -> str:
        """Pergunta pelo vínculo; devolve pending, approved, expired ou claimed.

        Quando aprovado, guarda o token e só então responde approved: o
        financeiro entrega o token uma única vez.
        """
        payload = self._link_request(
            "GET", f"/api/v1/telegram/link-requests/{code}", {200, 202, 404, 410}
        )
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        status = data.get("status", "expired")
        if status == "approved" and data.get("token") and self.token_store:
            self.token_store.save(role, data["token"])
            return "approved"
        return status if status in {"pending", "expired", "claimed"} else "expired"

    def _link_request(self, method: str, path: str, accepted: set[int]) -> dict:
        if not self.can_link():
            raise FinanceGatewayError("O vínculo por link do financeiro não está configurado.")
        try:
            response = requests.request(
                method,
                f"{self.base_url}{path}",
                headers={
                    "X-Telegram-Link-Secret": self.link_secret,
                    "Accept": "application/json",
                },
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as error:
            raise FinanceGatewayError(
                "Não consegui acessar o financeiro agora. Tente novamente."
            ) from error
        if response.status_code == 404 and 404 not in accepted:
            raise FinanceGatewayError("O vínculo por link está desligado no financeiro.")
        if response.status_code not in accepted:
            raise FinanceGatewayError("O financeiro recusou o pedido de vínculo.")
        if response.status_code == 404:
            return {"data": {"status": "expired"}}
        try:
            payload = response.json()
        except ValueError as error:
            raise FinanceGatewayError(
                "O financeiro devolveu uma resposta inesperada."
            ) from error
        return payload if isinstance(payload, dict) else {}

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
        token = self.token_for(role)
        if not self.base_url or not token:
            raise FinanceGatewayError(
                f"O financeiro de {role} ainda não está conectado. "
                "Envie /conectar_financeiro."
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
                "A autorização do financeiro expirou ou foi revogada. "
                "Envie /conectar_financeiro para conectar de novo."
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
