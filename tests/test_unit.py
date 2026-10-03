import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import jwt
from starlette.requests import Request

from app.config import get_settings
from app.services import payment_service
from app.services.auth_service import AuthService
from app.utilities.security import encrypt_password, verify_password


def test_password_hash_round_trip_and_rejects_wrong_password():
    password_hash = encrypt_password("correct horse")

    assert password_hash != "correct horse"
    assert verify_password("correct horse", password_hash)
    assert not verify_password("wrong horse", password_hash)


def test_authenticate_user_returns_token_for_valid_credentials(monkeypatch):
    user = SimpleNamespace(
        id=7,
        username="alice",
        password=encrypt_password("secret"),
        role="regular_user",
    )

    class FakeUserRepository:
        def get_by_username(self, username):
            return user if username == "alice" else None

    monkeypatch.setattr(
        "app.services.auth_service.create_access_token",
        lambda data: {"sub": data["sub"], "role": data["role"]},
    )

    token = AuthService(FakeUserRepository()).authenticate_user("alice", "secret")

    assert token == {"sub": "7", "role": "regular_user"}


def test_authenticate_user_returns_none_for_invalid_credentials():
    user = SimpleNamespace(
        id=7,
        username="alice",
        password=encrypt_password("secret"),
        role="regular_user",
    )

    class FakeUserRepository:
        def get_by_username(self, username):
            return user if username == "alice" else None

    service = AuthService(FakeUserRepository())

    assert service.authenticate_user("alice", "incorrect") is None
    assert service.authenticate_user("missing", "secret") is None


def test_access_token_contains_subject_and_role():
    from app.config import get_settings
    from app.utilities.security import create_access_token

    token = create_access_token({"sub": "7", "role": "admin"})
    settings = get_settings()
    payload = jwt.decode(
        token,
        settings.secret_key,
        algorithms=[settings.jwt_algorithm],
    )

    assert payload["sub"] == "7"
    assert payload["role"] == "admin"
    assert "exp" in payload


def test_stripe_checkout_request_and_payment_verification(monkeypatch):
    real_async_client = httpx.AsyncClient

    def provider_response(request):
        if request.method == "POST":
            data = parse_qs(request.content.decode())
            assert data["line_items[0][price_data][unit_amount]"] == ["1234"]
            assert data["metadata[user_id]"] == ["7"]
            assert data["metadata[listing_id]"] == ["42"]
            return httpx.Response(
                200,
                json={"url": "https://checkout.stripe.test/session"},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "id": "cs_test_123",
                "mode": "payment",
                "payment_status": "paid",
                "currency": "usd",
                "amount_total": 1234,
                "payment_intent": "pi_test_123",
                "metadata": {"user_id": "7", "listing_id": "42"},
            },
            request=request,
        )

    transport = httpx.MockTransport(provider_response)
    monkeypatch.setattr(
        payment_service.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, **kwargs),
    )
    settings = get_settings().model_copy(update={"stripe_secret_key": "sk_test_example"})
    listing = SimpleNamespace(id=42, price=12.34, game=SimpleNamespace(title="Test Game"))
    user = SimpleNamespace(id=7)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("games.example.test", 443),
            "path": "/checkout/stripe",
            "query_string": b"",
            "headers": [],
        }
    )

    checkout_url = asyncio.run(
        payment_service.create_stripe_checkout(settings, request, listing, user)
    )
    verified = asyncio.run(
        payment_service.verify_stripe_checkout(settings, "cs_test_123", user.id)
    )

    assert checkout_url == "https://checkout.stripe.test/session"
    assert verified.user_id == 7
    assert verified.listing_id == 42
    assert verified.amount == 12.34
    assert verified.transaction_id == "pi_test_123"


def test_paypal_checkout_and_capture_are_verified(monkeypatch):
    real_async_client = httpx.AsyncClient

    def provider_response(request):
        if request.url.path.endswith("/v1/oauth2/token"):
            return httpx.Response(200, json={"access_token": "sandbox-token"}, request=request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "ORDER-42",
                    "status": "APPROVED",
                    "purchase_units": [
                        {
                            "custom_id": "7:42",
                            "amount": {"currency_code": "USD", "value": "12.34"},
                        }
                    ],
                },
                request=request,
            )
        if request.url.path.endswith("/capture"):
            return httpx.Response(
                201,
                json={
                    "status": "COMPLETED",
                    "purchase_units": [
                        {
                            "payments": {
                                "captures": [
                                    {
                                        "id": "CAPTURE-42",
                                        "status": "COMPLETED",
                                        "amount": {"currency_code": "USD", "value": "12.34"},
                                    }
                                ]
                            }
                        }
                    ],
                },
                request=request,
            )
        body = request.read()
        assert b'"custom_id":"7:42"' in body
        return httpx.Response(
            201,
            json={
                "links": [{"rel": "approve", "href": "https://paypal.test/approve"}]
            },
            request=request,
        )

    transport = httpx.MockTransport(provider_response)
    monkeypatch.setattr(
        payment_service.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, **kwargs),
    )
    settings = get_settings().model_copy(
        update={
            "paypal_client_id": "sandbox-client",
            "paypal_client_secret": "sandbox-secret",
            "paypal_api_base_url": "https://api-m.sandbox.paypal.com",
        }
    )
    listing = SimpleNamespace(id=42, price=12.34, game=SimpleNamespace(title="Test Game"))
    user = SimpleNamespace(id=7)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("games.example.test", 443),
            "path": "/checkout/paypal",
            "query_string": b"",
            "headers": [],
        }
    )

    checkout_url = asyncio.run(
        payment_service.create_paypal_checkout(settings, request, listing, user)
    )
    verified = asyncio.run(
        payment_service.capture_paypal_checkout(settings, "ORDER-42", user.id)
    )

    assert checkout_url == "https://paypal.test/approve"
    assert verified.user_id == 7
    assert verified.listing_id == 42
    assert verified.amount == 12.34
    assert verified.transaction_id == "CAPTURE-42"
