from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.domain import Game, Listing, Rental


def get_listings(
    db: Session,
    platform: str | None = None,
    *,
    available_only: bool = False,
    confirmed_only: bool = False,
) -> list[Listing]:
    query = select(Listing).join(Game, Listing.game_id == Game.id)
    if platform:
        query = query.where(Game.platform == platform)
    if available_only:
        query = query.where(Listing.available.is_(True))
    if confirmed_only:
        query = query.where(Listing.confirmed.is_(True))
    return db.exec(query.order_by(Game.title)).all()


def get_pending_listings(db: Session) -> list[Listing]:
    return db.exec(
        select(Listing)
        .where(Listing.confirmed.is_(False))
        .order_by(Listing.id)
    ).all()


def confirm_listing(db: Session, listing_id: int) -> tuple[Listing, bool]:
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="listing not found")
    if listing.confirmed:
        return listing, False
    listing.confirmed = True
    db.add(listing)
    db.commit()
    db.refresh(listing)
    return listing, True


def get_pending_rentals(db: Session) -> list[Rental]:
    return db.exec(
        select(Rental)
        .where(Rental.confirmed.is_(False))
        .order_by(Rental.rental_date, Rental.id)
    ).all()


def confirm_rental(db: Session, rental_id: int) -> tuple[Rental, bool]:
    rental = db.get(Rental, rental_id)
    if rental is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="rental not found")
    if rental.return_date is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="rental was already returned")
    if rental.confirmed:
        return rental, False
    rental.confirmed = True
    db.add(rental)
    db.commit()
    db.refresh(rental)
    return rental, True


def get_pending_returns(db: Session) -> list[Rental]:
    return db.exec(
        select(Rental)
        .where(
            Rental.return_requested_date.is_not(None),
            Rental.return_date.is_(None),
        )
        .order_by(Rental.return_requested_date, Rental.id)
    ).all()


def request_rental_return(
    db: Session,
    rental_id: int,
    customer_id: int,
) -> tuple[Rental, bool]:
    rental = db.get(Rental, rental_id)
    if rental is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="rental not found")
    if rental.customer_id != customer_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="rental belongs to another user")
    if not rental.confirmed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="rental is awaiting staff confirmation",
        )
    if rental.return_date is not None or rental.return_requested_date is not None:
        return rental, False

    rental.return_requested_date = datetime.now(timezone.utc).date()
    db.add(rental)
    db.commit()
    db.refresh(rental)
    return rental, True


def confirm_rental_return(
    db: Session,
    rental_id: int,
) -> tuple[Rental, bool]:
    rental = db.get(Rental, rental_id)
    if rental is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="rental not found")
    if rental.return_date is not None:
        return rental, False
    if rental.return_requested_date is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="rental has no pending return request",
        )

    rental.return_date = datetime.now(timezone.utc).date()
    listing = db.get(Listing, rental.listing_id)
    if listing is not None:
        listing.available = True
        db.add(listing)
    db.add(rental)
    db.commit()
    db.refresh(rental)
    return rental, True