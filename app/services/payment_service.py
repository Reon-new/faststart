from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx
from fastapi import Request

from app.config import Settings
from app.models.domain import Listing
from app.models.user import User


class PaymentConfigurationError(RuntimeError):
    pass


class PaymentGatewayError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedCheckout:
    user_id: int
    listing_id: int
    amount: float
    transaction_id: str


def _json_response(response: httpx.Response) -> dict:
    try:
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PaymentGatewayError("The payment provider could not complete the request.") from exc


async def _provider_request(
    client: httpx.AsyncClient, method: str, url: str, **kwargs
) -> dict:
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise PaymentGatewayError("The payment provider could not be reached.") from exc
    return _json_response(response)


async def create_stripe_checkout(
    settings: Settings, request: Request, listing: Listing, user: User
) -> str:
    if not settings.stripe_secret_key:
        raise PaymentConfigurationError("Stripe checkout is not configured.")

    base_url = str(request.base_url).rstrip("/")
    data = {
        "mode": "payment",
        "success_url": f"{base_url}/payments/stripe/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base_url}/payments/cancel",
        "client_reference_id": str(listing.id),
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(round(listing.price * 100)),
        "line_items[0][price_data][product_data][name]": listing.game.title,
        "metadata[user_id]": str(user.id),
        "metadata[listing_id]": str(listing.id),
    }
    async with httpx.AsyncClient(timeout=20) as client:
        checkout = await _provider_request(
            client,
            "POST",
            "https://api.stripe.com/v1/checkout/sessions",
            data=data,
            auth=(settings.stripe_secret_key, ""),
        )
    checkout_url = checkout.get("url", "")
    if urlsplit(checkout_url).scheme != "https":
        raise PaymentGatewayError("Stripe did not return a valid checkout URL.")
    return checkout_url


async def verify_stripe_checkout(
    settings: Settings, session_id: str, user_id: int
) -> VerifiedCheckout:
    if not settings.stripe_secret_key:
        raise PaymentConfigurationError("Stripe checkout is not configured.")

    async with httpx.AsyncClient(timeout=20) as client:
        checkout = await _provider_request(
            client,
            "GET",
            f"https://api.stripe.com/v1/checkout/sessions/{quote(session_id, safe='')}",
            auth=(settings.stripe_secret_key, ""),
        )
    metadata = checkout.get("metadata") or {}
    try:
        checkout_user_id = int(metadata["user_id"])
        listing_id = int(metadata["listing_id"])
        amount = int(checkout["amount_total"]) / 100
    except (KeyError, TypeError, ValueError) as exc:
        raise PaymentGatewayError("Stripe checkout details are incomplete.") from exc

    if (
        checkout.get("payment_status") != "paid"
        or checkout.get("mode") != "payment"
        or checkout.get("currency") != "usd"
        or checkout_user_id != user_id
    ):
        raise PaymentGatewayError("Stripe payment could not be verified.")

    payment_intent = checkout.get("payment_intent")
    if isinstance(payment_intent, dict):
        payment_intent = payment_intent.get("id")
    return VerifiedCheckout(
        user_id=checkout_user_id,
        listing_id=listing_id,
        amount=amount,
        transaction_id=str(payment_intent or checkout.get("id") or session_id),
    )


async def _paypal_access_token(settings: Settings, client: httpx.AsyncClient) -> str:
    if not settings.paypal_client_id or not settings.paypal_client_secret:
        raise PaymentConfigurationError("PayPal checkout is not configured.")
    data = await _provider_request(
        client,
        "POST",
        f"{settings.paypal_api_base_url.rstrip('/')}/v1/oauth2/token",
        data={"grant_type": "client_credentials"},
        auth=(settings.paypal_client_id, settings.paypal_client_secret),
    )
    token = data.get("access_token")
    if not token:
        raise PaymentGatewayError("PayPal authentication failed.")
    return token


async def create_paypal_checkout(
    settings: Settings, request: Request, listing: Listing, user: User
) -> str:
    async with httpx.AsyncClient(timeout=20) as client:
        access_token = await _paypal_access_token(settings, client)
        order = await _provider_request(
            client,
            "POST",
            f"{settings.paypal_api_base_url.rstrip('/')}/v2/checkout/orders",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "intent": "CAPTURE",
                "purchase_units": [
                    {
                        "custom_id": f"{user.id}:{listing.id}",
                        "description": listing.game.title,
                        "amount": {
                            "currency_code": "USD",
                            "value": f"{listing.price:.2f}",
                        },
                    }
                ],
                "application_context": {
                    "return_url": str(request.base_url).rstrip("/")
                    + "/payments/paypal/success",
                    "cancel_url": str(request.base_url).rstrip("/") + "/payments/cancel",
                    "user_action": "PAY_NOW",
                },
            },
        )
    approval_url = next(
        (link.get("href") for link in order.get("links", []) if link.get("rel") == "approve"),
        "",
    )
    if urlsplit(approval_url).scheme != "https":
        raise PaymentGatewayError("PayPal did not return a valid checkout URL.")
    return approval_url


async def capture_paypal_checkout(
    settings: Settings, order_id: str, user_id: int
) -> VerifiedCheckout:
    base_url = settings.paypal_api_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=20) as client:
        access_token = await _paypal_access_token(settings, client)
        headers = {"Authorization": f"Bearer {access_token}"}
        encoded_order_id = quote(order_id, safe="")
        order = await _provider_request(
            client,
            "GET",
            f"{base_url}/v2/checkout/orders/{encoded_order_id}",
            headers=headers,
        )

        try:
            purchase = order["purchase_units"][0]
            custom_user_id, listing_id = map(int, purchase["custom_id"].split(":", 1))
            order_amount = float(purchase["amount"]["value"])
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise PaymentGatewayError("PayPal order details are incomplete.") from exc

        if (
            custom_user_id != user_id
            or purchase["amount"].get("currency_code") != "USD"
            or order.get("status") not in {"APPROVED", "COMPLETED"}
        ):
            raise PaymentGatewayError("PayPal payment could not be verified.")

        if order.get("status") == "APPROVED":
            order = await _provider_request(
                client,
                "POST",
                f"{base_url}/v2/checkout/orders/{encoded_order_id}/capture",
                headers=headers,
                json={},
            )

    captures = (
        order.get("purchase_units", [{}])[0]
        .get("payments", {})
        .get("captures", [])
    )
    capture = next((item for item in captures if item.get("status") == "COMPLETED"), None)
    if capture is None:
        raise PaymentGatewayError("PayPal payment has not been captured.")
    try:
        captured_amount = float(capture["amount"]["value"])
        capture_currency = capture["amount"]["currency_code"]
    except (KeyError, TypeError, ValueError) as exc:
        raise PaymentGatewayError("PayPal capture details are incomplete.") from exc
    if capture_currency != "USD" or round(captured_amount * 100) != round(order_amount * 100):
        raise PaymentGatewayError("PayPal capture amount does not match the order.")

    return VerifiedCheckout(
        user_id=custom_user_id,
        listing_id=listing_id,
        amount=captured_amount,
        transaction_id=str(capture["id"]),
    )