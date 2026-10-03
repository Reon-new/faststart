from datetime import date, datetime, timezone

from sqlmodel import Field, Relationship, SQLModel

from app.models.user import User


class Customer(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, foreign_key="user.id", unique=True)
    status: str = "Active"


class Staff(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, foreign_key="user.id", unique=True)
    title: str = "Staff"


class Owner(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, foreign_key="user.id", unique=True)
    commission_rate: float = 0.10


class Game(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str = Field(index=True)
    rating: str = "E"
    platform: str = Field(index=True)
    boxart: str | None = None
    genre: str | None = None
    listings: list["Listing"] = Relationship(back_populates="game")


class Listing(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    game_id: int = Field(foreign_key="game.id")
    owner_id: int | None = Field(default=None, foreign_key="user.id")
    condition: str = "new"
    price: float = 0.0
    available: bool = True
    confirmed: bool = False
    game: Game | None = Relationship(back_populates="listings")
    owner: User | None = Relationship(back_populates="listings")
    rentals: list["Rental"] = Relationship(back_populates="listing")


class Payment(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    customer_id: int | None = Field(default=None, foreign_key="user.id")
    payment_date: date = Field(default_factory=lambda: datetime.now(timezone.utc).date())
    amount: float = 0.0
    customer: User | None = Relationship(back_populates="payments")
    rentals: list["Rental"] = Relationship(back_populates="payment")


class Rental(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    listing_id: int = Field(foreign_key="listing.id")
    customer_id: int = Field(foreign_key="user.id")
    payment_id: int | None = Field(default=None, foreign_key="payment.id")
    rental_date: date = Field(default_factory=lambda: datetime.now(timezone.utc).date())
    confirmed: bool = False
    return_requested_date: date | None = None
    return_date: date | None = None
    listing: Listing | None = Relationship(back_populates="rentals")
    customer: User | None = Relationship(back_populates="rentals")
    payment: Payment | None = Relationship(back_populates="rentals")


class RentalPayment(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    rental_id: int = Field(foreign_key="rental.id")
    payment_id: int = Field(foreign_key="payment.id")
