from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import select

from app.config import get_settings
from app.dependencies.auth import AuthDep
from app.dependencies.session import SessionDep
from app.models.domain import Game, Listing, Rental
from app.services.marketplace_service import (
    confirm_listing,
    confirm_rental,
    confirm_rental_return,
    get_listings,
    get_pending_listings,
    get_pending_rentals,
    get_pending_returns,
    request_rental_return,
)
from app.utilities.flash import flash

from . import router, templates


def _staff_home_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(request.url_for("user_home_view"), status_code=303)


@router.get("/app", response_class=HTMLResponse)
async def user_home_view(
    request: Request,
    user: AuthDep,
    db: SessionDep,
    platform: str | None = None,
):
    if user.role == "staff":
        return templates.TemplateResponse(
            request=request,
            name="staff-home.html",
            context={"user": user},
        )

    listings = get_listings(db, platform, available_only=True, confirmed_only=True)
    platforms = db.exec(select(Game.platform).distinct().order_by(Game.platform)).all()
    settings = get_settings()
    return templates.TemplateResponse(
        request=request, 
        name="app.html",
        context={
            "user": user,
            "listings": listings,
            "platforms": platforms,
            "selected_platform": platform or "",
            "stripe_enabled": bool(settings.stripe_secret_key),
            "paypal_enabled": bool(settings.paypal_client_id and settings.paypal_client_secret),
        }
    )


@router.get("/rentals", response_class=HTMLResponse)
async def user_rentals_view(
    request: Request,
    user: AuthDep,
    db: SessionDep,
):
    if user.role == "staff":
        return _staff_home_redirect(request)

    rentals = db.exec(
        select(Rental)
        .where(Rental.customer_id == user.id)
        .order_by(Rental.rental_date.desc(), Rental.id.desc())
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="rentals.html",
        context={"user": user, "rentals": rentals},
    )


@router.post("/rentals/{rental_id}/return", name="request_rental_return")
async def request_rental_return_view(
    rental_id: int,
    request: Request,
    user: AuthDep,
    db: SessionDep,
):
    if user.role == "staff":
        return _staff_home_redirect(request)

    _, newly_requested = request_rental_return(db, rental_id, user.id)
    message = (
        "Return request sent to staff."
        if newly_requested
        else "A return request is already pending or this game was returned."
    )
    flash(request, message, "success")
    return RedirectResponse(request.url_for("user_rentals_view"), status_code=303)


@router.get("/listings/new", response_class=HTMLResponse)
async def new_listing_view(request: Request, user: AuthDep):
    if user.role == "staff":
        return _staff_home_redirect(request)

    return templates.TemplateResponse(
        request=request,
        name="listing-create.html",
        context={"user": user},
    )


@router.get("/my-listings", response_class=HTMLResponse)
async def my_listings_view(
    request: Request,
    user: AuthDep,
    db: SessionDep,
):
    if user.role == "staff":
        return _staff_home_redirect(request)

    listings = db.exec(
        select(Listing)
        .where(Listing.owner_id == user.id)
        .order_by(Listing.id.desc())
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="my-listings.html",
        context={"user": user, "listings": listings},
    )


@router.get("/staff/listings", response_class=HTMLResponse)
async def staff_listings_view(
    request: Request,
    user: AuthDep,
    db: SessionDep,
):
    if user.role != "staff":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="staff access required")
    return templates.TemplateResponse(
        request=request,
        name="staff-listings.html",
        context={"user": user, "listings": get_pending_listings(db)},
    )


@router.post("/staff/listings/{listing_id}/confirm", name="staff_confirm_listing")
async def staff_confirm_listing_view(
    listing_id: int,
    request: Request,
    user: AuthDep,
    db: SessionDep,
):
    if user.role != "staff":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="staff access required")
    _, newly_confirmed = confirm_listing(db, listing_id)
    message = "Listing approved." if newly_confirmed else "Listing was already approved."
    flash(request, message, "success")
    return RedirectResponse(request.url_for("staff_listings_view"), status_code=303)


@router.get("/staff/rentals", response_class=HTMLResponse)
async def staff_rentals_view(
    request: Request,
    user: AuthDep,
    db: SessionDep,
):
    if user.role != "staff":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="staff access required")
    return templates.TemplateResponse(
        request=request,
        name="staff-rentals.html",
        context={"user": user, "rentals": get_pending_rentals(db)},
    )


@router.post("/staff/rentals/{rental_id}/confirm", name="staff_confirm_rental")
async def staff_confirm_rental_view(
    rental_id: int,
    request: Request,
    user: AuthDep,
    db: SessionDep,
):
    if user.role != "staff":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="staff access required")
    _, newly_confirmed = confirm_rental(db, rental_id)
    message = "Rental approved." if newly_confirmed else "Rental was already approved."
    flash(request, message, "success")
    return RedirectResponse(request.url_for("staff_rentals_view"), status_code=303)


@router.get("/staff/returns", response_class=HTMLResponse)
async def staff_returns_view(
    request: Request,
    user: AuthDep,
    db: SessionDep,
):
    if user.role != "staff":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="staff access required")
    return templates.TemplateResponse(
        request=request,
        name="staff-returns.html",
        context={"user": user, "rentals": get_pending_returns(db)},
    )


@router.post("/staff/rentals/{rental_id}/confirm-return", name="staff_confirm_return")
async def staff_confirm_return_view(
    rental_id: int,
    request: Request,
    user: AuthDep,
    db: SessionDep,
):
    if user.role != "staff":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="staff access required")
    _, newly_returned = confirm_rental_return(db, rental_id)
    message = "Return confirmed; the game is available again." if newly_returned else "Return was already confirmed."
    flash(request, message, "success")
    return RedirectResponse(request.url_for("staff_returns_view"), status_code=303)