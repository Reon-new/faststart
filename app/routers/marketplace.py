from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlmodel import select

from app.config import get_settings
from app.dependencies import AuthDep, SessionDep
from app.models.domain import Game, Listing, Payment, Rental
from app.models.user import User
from app.schemas.auth import SigninRequest, SignupRequest
from app.schemas.market import (
    GameCreate,
    ListingCreate,
    PaymentCreate,
    RentalCreate,
    RentalUpdate,
)
from app.services.marketplace_service import (
    confirm_listing,
    confirm_rental,
    confirm_rental_return,
    get_listings,
    request_rental_return,
)
from app.services.payment_service import (
    PaymentConfigurationError,
    PaymentGatewayError,
    VerifiedCheckout,
    capture_paypal_checkout,
    create_paypal_checkout,
    create_stripe_checkout,
    verify_stripe_checkout,
)
from app.utilities.flash import flash
from app.utilities.security import encrypt_password

from . import api_router, router


@router.post("/signup", status_code=status.HTTP_201_CREATED)
@api_router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(payload: SignupRequest, db: SessionDep):
    existing_user = db.exec(
        select(User).where((User.username == payload.username) | (User.email == payload.email))
    ).first()
    if existing_user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="duplicate user")

    user = User(
        username=payload.username,
        email=payload.email,
        password=encrypt_password(payload.password),
        role="regular_user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "username": user.username, "email": user.email, "role": user.role}


@router.post("/auth")
@api_router.post("/auth")
async def auth(payload: SigninRequest, db: SessionDep):
    user = db.exec(select(User).where(User.username == payload.username)).first()
    if user is None or not user.check_password(payload.password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid credentials")

    from app.utilities.security import create_access_token

    token = create_access_token({"sub": str(user.id), "role": user.role})
    return {"token": token, "user_id": user.id, "role": user.role}


@router.post("/games", status_code=status.HTTP_201_CREATED)
@api_router.post("/games", status_code=status.HTTP_201_CREATED)
async def create_game(payload: GameCreate, db: SessionDep, user: AuthDep):
    game = Game(
        title=payload.title,
        rating=payload.rating,
        platform=payload.platform,
        boxart=payload.boxart,
        genre=payload.genre,
    )
    db.add(game)
    db.commit()
    db.refresh(game)
    return {
        "id": game.id,
        "title": game.title,
        "rating": game.rating,
        "platform": game.platform,
        "boxart": game.boxart,
        "genre": game.genre,
    }


@router.post("/listings", status_code=status.HTTP_201_CREATED)
@api_router.post("/listings", status_code=status.HTTP_201_CREATED)
async def create_listing(payload: ListingCreate, db: SessionDep, user: AuthDep):
    if not db.get(Game, payload.game_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="game not found")

    listing = Listing(
        game_id=payload.game_id,
        owner_id=user.id,
        condition=payload.condition,
        price=payload.price,
        available=True,
        confirmed=False,
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)
    return {
        "id": listing.id,
        "game_id": listing.game_id,
        "owner_id": listing.owner_id,
        "condition": listing.condition,
        "price": listing.price,
        "available": listing.available,
        "confirmed": listing.confirmed,
    }


@router.get("/listings")
@api_router.get("/listings")
async def list_listings(platform: str | None = None, db: SessionDep = None):
    rows = get_listings(db, platform, confirmed_only=True)
    result = []
    for row in rows:
        result.append(
            {
                "id": row.id,
                "game_id": row.game_id,
                "owner_id": row.owner_id,
                "condition": row.condition,
                "price": row.price,
                "available": row.available,
                "confirmed": row.confirmed,
                "game": {
                    "id": row.game.id,
                    "title": row.game.title,
                    "rating": row.game.rating,
                    "platform": row.game.platform,
                    "boxart": row.game.boxart,
                    "genre": row.game.genre,
                },
            }
        )
    return result


@api_router.post("/listings/{listing_id}/confirm")
async def confirm_listing_api(listing_id: int, db: SessionDep, user: AuthDep):
    if user.role != "staff":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="staff access required",
        )
    listing, newly_confirmed = confirm_listing(db, listing_id)
    return {
        "id": listing.id,
        "game_id": listing.game_id,
        "owner_id": listing.owner_id,
        "condition": listing.condition,
        "price": listing.price,
        "available": listing.available,
        "confirmed": listing.confirmed,
        "newly_confirmed": newly_confirmed,
    }


@router.post("/payment", status_code=status.HTTP_201_CREATED)
@api_router.post("/payment", status_code=status.HTTP_201_CREATED)
async def create_payment(payload: PaymentCreate, db: SessionDep):
    payment = Payment(
        customer_id=payload.customer_id,
        amount=payload.amount,
        payment_date=datetime.now(timezone.utc).date(),
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return {"id": payment.id, "customer_id": payment.customer_id, "amount": payment.amount, "payment_date": payment.payment_date}


@router.post("/checkout/{provider}", name="begin_checkout")
async def begin_checkout(
    provider: Literal["stripe", "paypal"],
    listing_id: Annotated[int, Form()],
    request: Request,
    db: SessionDep,
    user: AuthDep,
):
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="listing not found")
    if not listing.available:
        flash(request, "This game is no longer available.", "warning")
        return RedirectResponse(request.url_for("user_home_view"), status_code=303)

    try:
        settings = get_settings()
        if provider == "stripe":
            checkout_url = await create_stripe_checkout(settings, request, listing, user)
        else:
            checkout_url = await create_paypal_checkout(settings, request, listing, user)
    except (PaymentConfigurationError, PaymentGatewayError) as exc:
        flash(request, str(exc), "danger")
        return RedirectResponse(request.url_for("user_home_view"), status_code=303)

    return RedirectResponse(checkout_url, status_code=303)


def _finish_paid_checkout(
    request: Request, db, user, checkout: VerifiedCheckout
) -> RedirectResponse:
    if checkout.user_id != user.id:
        flash(request, "The payment belongs to a different account.", "danger")
        return RedirectResponse(request.url_for("user_home_view"), status_code=303)

    existing_rental = db.exec(
        select(Rental).where(
            Rental.listing_id == checkout.listing_id,
            Rental.customer_id == user.id,
        )
    ).first()
    if existing_rental and existing_rental.payment_id:
        flash(request, "This rental has already been confirmed.", "success")
        return RedirectResponse(request.url_for("user_home_view"), status_code=303)

    listing = db.get(Listing, checkout.listing_id)
    if listing is None or not listing.available:
        flash(
            request,
            "Payment was received, but this game is no longer available. Please contact support.",
            "danger",
        )
        return RedirectResponse(request.url_for("user_home_view"), status_code=303)
    if round(checkout.amount * 100) != round(listing.price * 100):
        flash(request, "The payment amount does not match this listing.", "danger")
        return RedirectResponse(request.url_for("user_home_view"), status_code=303)

    payment = Payment(
        customer_id=user.id,
        amount=listing.price,
        payment_date=datetime.now(timezone.utc).date(),
    )
    db.add(payment)
    db.flush()
    rental = Rental(
        listing_id=listing.id,
        customer_id=user.id,
        payment_id=payment.id,
        rental_date=datetime.now(timezone.utc).date(),
    )
    listing.available = False
    db.add(listing)
    db.add(rental)
    db.commit()
    flash(
        request,
        f"Payment complete. Rental request #{rental.id} was sent to staff for confirmation.",
        "success",
    )
    return RedirectResponse(request.url_for("user_home_view"), status_code=303)


@router.get("/payments/stripe/success", name="stripe_checkout_success")
async def stripe_checkout_success(
    request: Request,
    session_id: str,
    db: SessionDep,
    user: AuthDep,
):
    try:
        checkout = await verify_stripe_checkout(get_settings(), session_id, user.id)
    except (PaymentConfigurationError, PaymentGatewayError) as exc:
        flash(request, str(exc), "danger")
        return RedirectResponse(request.url_for("user_home_view"), status_code=303)
    return _finish_paid_checkout(request, db, user, checkout)


@router.get("/payments/paypal/success", name="paypal_checkout_success")
async def paypal_checkout_success(
    request: Request,
    token: str,
    db: SessionDep,
    user: AuthDep,
):
    try:
        checkout = await capture_paypal_checkout(get_settings(), token, user.id)
    except (PaymentConfigurationError, PaymentGatewayError) as exc:
        flash(request, str(exc), "danger")
        return RedirectResponse(request.url_for("user_home_view"), status_code=303)
    return _finish_paid_checkout(request, db, user, checkout)


@router.get("/payments/cancel", name="checkout_cancelled")
async def checkout_cancelled(request: Request):
    flash(request, "Checkout was cancelled. No rental was created.", "info")
    return RedirectResponse(request.url_for("user_home_view"), status_code=303)


@router.post("/rentals", status_code=status.HTTP_201_CREATED)
@api_router.post("/rentals", status_code=status.HTTP_201_CREATED)
async def create_rental(payload: RentalCreate, db: SessionDep, user: AuthDep):
    listing = db.get(Listing, payload.listing_id)
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bad listing")
    if not listing.available:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="listing is unavailable")
    if not listing.confirmed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="listing is awaiting staff confirmation")

    rental = Rental(
        listing_id=payload.listing_id,
        customer_id=user.id,
        payment_id=payload.payment_id,
        rental_date=datetime.now(timezone.utc).date(),
        confirmed=False,
    )
    listing.available = False
    db.add(listing)
    db.add(rental)
    db.commit()
    db.refresh(rental)
    return {"id": rental.id, "listing_id": rental.listing_id, "customer_id": rental.customer_id, "payment_id": rental.payment_id, "rental_date": rental.rental_date, "confirmed": rental.confirmed}


@router.put("/rentals/{rental_id}")
@api_router.put("/rentals/{rental_id}")
async def update_rental(rental_id: int, payload: RentalUpdate, db: SessionDep):
    rental = db.get(Rental, rental_id)
    if rental is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="rental not found")
    if payload.payment_id is not None:
        payment = db.get(Payment, payload.payment_id)
        if payment is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="payment not found")
        rental.payment_id = payload.payment_id
    db.add(rental)
    db.commit()
    db.refresh(rental)
    return {"id": rental.id, "listing_id": rental.listing_id, "customer_id": rental.customer_id, "payment_id": rental.payment_id, "rental_date": rental.rental_date, "return_date": rental.return_date}


@api_router.post("/rentals/{rental_id}/return")
async def request_rental_return_api(rental_id: int, db: SessionDep, user: AuthDep):
    rental, newly_requested = request_rental_return(db, rental_id, user.id)
    return {
        "id": rental.id,
        "listing_id": rental.listing_id,
        "customer_id": rental.customer_id,
        "payment_id": rental.payment_id,
        "rental_date": rental.rental_date,
        "confirmed": rental.confirmed,
        "return_requested_date": rental.return_requested_date,
        "return_date": rental.return_date,
        "newly_requested": newly_requested,
    }


@api_router.post("/rentals/{rental_id}/confirm")
async def confirm_rental_api(rental_id: int, db: SessionDep, user: AuthDep):
    if user.role != "staff":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="staff access required",
        )
    rental, newly_confirmed = confirm_rental(db, rental_id)
    return {
        "id": rental.id,
        "listing_id": rental.listing_id,
        "customer_id": rental.customer_id,
        "payment_id": rental.payment_id,
        "rental_date": rental.rental_date,
        "confirmed": rental.confirmed,
        "newly_confirmed": newly_confirmed,
    }


@api_router.post("/rentals/{rental_id}/confirm-return")
async def confirm_rental_return_api(rental_id: int, db: SessionDep, user: AuthDep):
    if user.role != "staff":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="staff access required",
        )
    rental, newly_returned = confirm_rental_return(db, rental_id)
    listing = db.get(Listing, rental.listing_id)
    return {
        "id": rental.id,
        "listing_id": rental.listing_id,
        "customer_id": rental.customer_id,
        "return_requested_date": rental.return_requested_date,
        "return_date": rental.return_date,
        "available": listing.available if listing else False,
        "newly_returned": newly_returned,
    }


@router.post("/listings/{listing_id}/sell")
@api_router.post("/listings/{listing_id}/sell")
async def sell_listing(listing_id: int, db: SessionDep):
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="listing not found")
    listing.available = False
    db.add(listing)
    db.commit()
    db.refresh(listing)
    return {"id": listing.id, "available": listing.available}
