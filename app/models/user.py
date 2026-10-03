from typing import Optional

from pydantic import EmailStr
from sqlmodel import Field, Relationship, SQLModel

from app.utilities.security import verify_password


class UserBase(SQLModel):
    username: str = Field(index=True, unique=True)
    email: EmailStr = Field(index=True, unique=True)
    password: str
    role: str = ""


class User(UserBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    listings: list["Listing"] = Relationship(back_populates="owner")
    rentals: list["Rental"] = Relationship(back_populates="customer")
    payments: list["Payment"] = Relationship(back_populates="customer")

    def check_password(self, plaintext_password: str) -> bool:
        return verify_password(plaintext_password, self.password)