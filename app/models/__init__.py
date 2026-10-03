"""Database table models.

Import every table model here so ``SQLModel.metadata.create_all`` sees them.
"""

from app.models.domain import Customer, Game, Listing, Owner, Payment, Rental, RentalPayment, Staff
from app.models.user import User

__all__ = [
    "User",
    "Customer",
    "Staff",
    "Owner",
    "Game",
    "Listing",
    "Payment",
    "Rental",
    "RentalPayment",
]
