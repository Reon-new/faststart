from datetime import date
from typing import Optional

from sqlmodel import SQLModel


class GameCreate(SQLModel):
    title: str
    rating: str = "E"
    platform: str
    boxart: Optional[str] = None
    genre: Optional[str] = None


class GameRead(GameCreate):
    id: int


class ListingCreate(SQLModel):
    game_id: int
    condition: str = "new"
    price: float = 0.0
    owner_id: Optional[int] = None


class ListingRead(SQLModel):
    id: int
    game_id: int
    owner_id: Optional[int] = None
    condition: str
    price: float
    available: bool = True
    game: Optional[GameRead] = None


class PaymentCreate(SQLModel):
    amount: float
    customer_id: Optional[int] = None


class PaymentRead(SQLModel):
    id: int
    amount: float
    customer_id: Optional[int] = None
    payment_date: date


class RentalCreate(SQLModel):
    listing_id: int
    customer_id: int
    payment_id: Optional[int] = None


class RentalUpdate(SQLModel):
    payment_id: Optional[int] = None


class RentalRead(SQLModel):
    id: int
    listing_id: int
    customer_id: int
    payment_id: Optional[int] = None
    rental_date: date
    return_date: Optional[date] = None
