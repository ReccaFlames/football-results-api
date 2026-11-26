import httpx
import pytest

from app import notify


@pytest.fixture(autouse=True)
def no_telegram_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never reach Telegram, even when `.env` configures it; tests that send replace this."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("tests must not reach Telegram")

    monkeypatch.setattr(
        notify,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
    )
