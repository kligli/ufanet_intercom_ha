"""Unit tests for Dom phone-auth helpers (empty 306, JSON contracts)."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types
from types import ModuleType
from typing import Any, Self

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "ufanet"


def _ensure_package() -> None:
    """Install a minimal ufanet package stub for relative imports."""
    if "ufanet" not in sys.modules:
        pkg = ModuleType("ufanet")
        pkg.__path__ = [str(PKG)]
        sys.modules["ufanet"] = pkg


def _load(name: str) -> ModuleType:
    """Load a package module from disk, replacing any cached copy."""
    _ensure_package()
    sys.modules.pop(f"ufanet.{name}", None)
    spec = importlib.util.spec_from_file_location(
        f"ufanet.{name}", PKG / f"{name}.py", submodule_search_locations=[str(PKG)]
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"ufanet.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_api() -> ModuleType:
    """Load api.py with real const and stubbed aiohttp (no Home Assistant)."""
    aiohttp_mod = sys.modules.get("aiohttp")
    if aiohttp_mod is None or not hasattr(aiohttp_mod, "ClientSession"):
        aiohttp = types.ModuleType("aiohttp")

        class ClientResponseError(Exception):
            """Stub aiohttp.ClientResponseError."""

            def __init__(self, *args: object, **kwargs: Any) -> None:
                self.status = kwargs.get("status", 0)
                super().__init__(*args)

        class ClientSession:
            """Stub aiohttp.ClientSession."""

        aiohttp.ClientResponseError = ClientResponseError
        aiohttp.ClientSession = ClientSession
        sys.modules["aiohttp"] = aiohttp

    _load("const")
    return _load("api")


class _FakeResponse:
    """Minimal aiohttp response stand-in."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeSession:
    """Captures the last POST and returns a scripted response."""

    def __init__(self, status: int, body: bytes) -> None:
        self._status = status
        self._body = body
        self.last_url: str | None = None
        self.last_json: dict[str, Any] | None = None
        self.last_headers: dict[str, str] | None = None

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeResponse:
        self.last_url = url
        self.last_json = json
        self.last_headers = headers
        return _FakeResponse(self._status, self._body)


@pytest.fixture
def api() -> ModuleType:
    """Return the loaded ufanet.api module under test."""
    return _load_api()


@pytest.mark.asyncio
async def test_contact_list_http_306_empty_returns_empty(api: ModuleType) -> None:
    """Empty HTTP 306 body (call not registered) → [] so the flow can poll."""
    session = _FakeSession(306, b"")
    contracts = await api.phone_auth_contact_list(
        session, "https://dom.ufanet.ru/", "req-1"
    )
    assert contracts == []
    assert session.last_headers == {"User-Agent": "okhttp/4.9.0"}
    assert session.last_json == {"request_id": "req-1"}
    assert session.last_url is not None
    assert session.last_url.endswith("api/v4/phone_auth/call/contact_list/")


@pytest.mark.asyncio
async def test_contact_list_json_contracts(api: ModuleType) -> None:
    """Normal JSON payload returns the contracts list."""
    payload = {
        "data": {
            "contracts": [
                {"id": 42, "title": "Договор 100"},
                {"id": 7, "title": "Договор 200"},
            ]
        }
    }
    session = _FakeSession(200, json.dumps(payload).encode())
    contracts = await api.phone_auth_contact_list(
        session, "https://dom.ufanet.ru/", "req-2"
    )
    assert contracts == payload["data"]["contracts"]


@pytest.mark.asyncio
async def test_init_rejects_empty_body(api: ModuleType) -> None:
    """Init must not treat an empty body as success."""
    session = _FakeSession(200, b"")
    with pytest.raises(api.PhoneAuthError, match="Empty response"):
        await api.phone_auth_init(session, "https://dom.ufanet.ru/", "+79001234567")


@pytest.mark.asyncio
async def test_init_parses_payload(api: ModuleType) -> None:
    """Init returns phone_to_call / request_id from data."""
    payload = {
        "data": {
            "request_id": "abc",
            "phone_to_call": "+74951234567",
            "timeout": 60,
        }
    }
    session = _FakeSession(200, json.dumps(payload).encode())
    result = await api.phone_auth_init(
        session, "https://dom.ufanet.ru/", "+79001234567"
    )
    assert result["request_id"] == "abc"
    assert result["phone_to_call"] == "+74951234567"
    assert session.last_headers == {"User-Agent": "okhttp/4.9.0"}
    assert session.last_json == {
        "phone": "+79001234567",
        "application_id": "ru.ufanet.smarthome",
        "country_id": 1,
    }


@pytest.mark.asyncio
async def test_apply_http_error(api: ModuleType) -> None:
    """Status >= 400 raises PhoneAuthError."""
    session = _FakeSession(500, b"boom")
    with pytest.raises(api.PhoneAuthError, match="500"):
        await api.phone_auth_apply(session, "https://dom.ufanet.ru/", "req", 1)


@pytest.mark.asyncio
async def test_contact_list_non_json_allow_empty(api: ModuleType) -> None:
    """Non-JSON body with allow_empty (via contact_list) → []."""
    session = _FakeSession(306, b"not-json")
    contracts = await api.phone_auth_contact_list(
        session, "https://dom.ufanet.ru/", "req-3"
    )
    assert contracts == []
