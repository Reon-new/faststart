from types import SimpleNamespace

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.config import get_settings
from app.database import get_session
from app.main import app
from app.models.domain import Game, Listing, Payment, Rental
from app.repositories.user import UserRepository
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.utilities.security import create_access_token, verify_password


def test_save_password(test_session):
    repository = UserRepository(test_session)
    service = AuthService(repository)

    created = service.register_user("alice", "alice@example.com", "secret")
    saved = repository.get_by_username("alice")

    assert saved is not None
    assert created.id == saved.id
    assert saved.username == "alice"
    assert saved.email == "alice@example.com"
    assert saved.role == "regular_user"
    assert saved.password != "secret"
    assert verify_password("secret", saved.password)


def test_login(test_session):
    repository = UserRepository(test_session)
    service = AuthService(repository)
    service.register_user("alice", "alice@example.com", "secret")

    token = service.authenticate_user("alice", "secret")
    settings = get_settings()
    payload = jwt.decode(
        token,
        settings.secret_key,
        algorithms=[settings.jwt_algorithm],
    )

    assert payload["sub"] == str(repository.get_by_username("alice").id)
    assert payload["role"] == "regular_user"


def test_register_user(test_session):
    repository = UserRepository(test_session)
    auth_service = AuthService(repository)
    auth_service.register_user("alice", "alice@example.com", "secret")

    users = UserService(repository).get_all_users()

    assert len(users) == 1
    assert users[0].username == "alice"
    assert users[0].email == "alice@example.com"


def test_marketplace_api_contract(test_session):
    app.dependency_overrides[get_session] = lambda: test_session
    client = TestClient(app)

    signup = client.post(
        "/signup",
        json={"username": "gamer", "email": "gamer@example.com", "password": "secret"},
    )
    assert signup.status_code == 201
    assert signup.json()["username"] == "gamer"

    auth = client.post("/auth", json={"username": "gamer", "password": "secret"})
    assert auth.status_code == 200
    assert "token" in auth.json()
    access_token = auth.json()["token"]
    client.cookies.set("access_token", access_token)

    game = client.post(
        "/games",
        json={
            "title": "Mario Kart 8",
            "rating": "E",
            "platform": "NSW",
            "boxart": "cover.png",
            "genre": "Racing",
        },
    )
    assert game.status_code == 201
    game_id = game.json()["id"]

    listing = client.post(
        "/listings",
        json={"game_id": game_id, "condition": "new", "price": 39.99},
    )
    assert listing.status_code == 201
    listing_id = listing.json()["id"]
    assert listing.json()["confirmed"] is False

    listings = client.get("/listings", params={"platform": "NSW"})
    assert listings.status_code == 200
    assert listings.json() == []

    staff = AuthService(UserRepository(test_session)).register_user(
        "listing-staff", "listing-staff@example.com", "secret"
    )
    staff.role = "staff"
    test_session.add(staff)
    test_session.commit()
    client.cookies.set(
        "access_token",
        create_access_token({"sub": str(staff.id), "role": staff.role}),
    )
    approval = client.post(f"/api/listings/{listing_id}/confirm")
    assert approval.status_code == 200
    assert approval.json()["confirmed"] is True
    client.cookies.set("access_token", access_token)
    listings = client.get("/listings", params={"platform": "NSW"})
    assert listings.json()[0]["game"]["platform"] == "NSW"

    payment = client.post("/payment", json={"amount": 39.99})
    assert payment.status_code == 201
    payment_id = payment.json()["id"]

    client.cookies.delete("access_token")
    unauthenticated_rental = client.post(
        "/rentals",
        json={"listing_id": listing_id, "customer_id": 1},
    )
    assert unauthenticated_rental.status_code == 401
    client.cookies.set("access_token", access_token)

    rental = client.post(
        "/rentals",
        json={"listing_id": listing_id, "customer_id": 1},
    )
    assert rental.status_code == 201
    assert rental.json()["confirmed"] is False
    assert rental.json()["customer_id"] == signup.json()["id"]
    rental_id = rental.json()["id"]
    duplicate_rental = client.post(
        "/rentals",
        json={"listing_id": listing_id, "customer_id": 1},
    )
    assert duplicate_rental.status_code == 409
    update = client.put(
        f"/rentals/{rental_id}",
        json={"payment_id": payment_id},
    )
    assert update.status_code == 200
    assert update.json()["payment_id"] == payment_id

    sold = client.post(f"/listings/{listing_id}/sell")
    assert sold.status_code == 200
    assert sold.json()["available"] is False

    app.dependency_overrides.clear()


def test_authenticated_home_browses_and_filters_available_games(test_session):
    user = AuthService(UserRepository(test_session)).register_user(
        "browser", "browser@example.com", "secret"
    )
    nsw_game = Game(title="Kart Racer", platform="NSW", rating="E", genre="Racing")
    ps5_game = Game(title="Space Adventure", platform="PS5", rating="T", genre="Adventure")
    test_session.add_all([nsw_game, ps5_game])
    test_session.commit()
    test_session.refresh(nsw_game)
    test_session.refresh(ps5_game)
    test_session.add_all(
        [
            Listing(
                game_id=nsw_game.id,
                owner_id=user.id,
                condition="good",
                price=20,
                confirmed=True,
            ),
            Listing(game_id=ps5_game.id, owner_id=user.id, condition="new", price=30, confirmed=True),
            Listing(
                game_id=ps5_game.id,
                owner_id=user.id,
                condition="good",
                price=25,
                available=False,
                confirmed=True,
            ),
        ]
    )
    test_session.commit()

    app.dependency_overrides[get_session] = lambda: test_session
    client = TestClient(app)
    token = create_access_token({"sub": str(user.id), "role": user.role})
    client.cookies.set("access_token", token)

    response = client.get("/app")
    assert response.status_code == 200
    assert "Kart Racer" in response.text
    assert "Space Adventure" in response.text
    assert 'data-bs-toggle="modal"' in response.text
    assert "modal-dialog-centered" in response.text
    assert "Rent this game" in response.text
    assert "Pay with Stripe" in response.text
    assert "Pay with PayPal" in response.text
    assert "Racing" in response.text
    assert "Listed by" in response.text
    assert "No games found" not in response.text

    filtered = client.get("/app?platform=PS5")
    assert filtered.status_code == 200
    assert "Space Adventure" in filtered.text
    assert "Kart Racer" not in filtered.text
    assert "No games found" not in filtered.text

    app.dependency_overrides.clear()


def test_authenticated_user_can_view_only_their_rentals(test_session):
    renter = AuthService(UserRepository(test_session)).register_user(
        "renter", "renter@example.com", "secret"
    )
    other_user = AuthService(UserRepository(test_session)).register_user(
        "other", "other@example.com", "secret"
    )
    first_game = Game(title="My Rental Game", platform="NSW", rating="E")
    other_game = Game(title="Private Other Game", platform="PS5", rating="T")
    test_session.add_all([first_game, other_game])
    test_session.commit()
    test_session.refresh(first_game)
    test_session.refresh(other_game)
    first_listing = Listing(game_id=first_game.id, owner_id=other_user.id, price=15)
    other_listing = Listing(game_id=other_game.id, owner_id=renter.id, price=25)
    test_session.add_all([first_listing, other_listing])
    test_session.commit()
    test_session.refresh(first_listing)
    test_session.refresh(other_listing)
    payment = Payment(customer_id=renter.id, amount=15)
    test_session.add(payment)
    test_session.commit()
    test_session.refresh(payment)
    test_session.add_all(
        [
            Rental(listing_id=first_listing.id, customer_id=renter.id, payment_id=payment.id),
            Rental(listing_id=other_listing.id, customer_id=other_user.id),
        ]
    )
    test_session.commit()

    app.dependency_overrides[get_session] = lambda: test_session
    client = TestClient(app)
    token = create_access_token({"sub": str(renter.id), "role": renter.role})
    client.cookies.set("access_token", token)

    response = client.get("/rentals")

    assert response.status_code == 200
    assert "My Rental Game" in response.text
    assert "Private Other Game" not in response.text
    assert "15.00" in response.text
    assert "Payment date" in response.text
    app.dependency_overrides.clear()


def test_user_can_confirm_their_rental_returns(test_session):
    renter = AuthService(UserRepository(test_session)).register_user(
        "returner", "returner@example.com", "secret"
    )
    other_user = AuthService(UserRepository(test_session)).register_user(
        "nonreturner", "nonreturner@example.com", "secret"
    )
    other_user.role = "staff"
    test_session.add(other_user)
    test_session.commit()
    games = [
        Game(title="Return by form", platform="NSW", rating="E"),
        Game(title="Return by API", platform="PS5", rating="T"),
        Game(title="Other person's rental", platform="PC", rating="E"),
    ]
    test_session.add_all(games)
    test_session.commit()
    for game in games:
        test_session.refresh(game)
    listings = [
        Listing(game_id=games[0].id, owner_id=other_user.id, price=10, available=False),
        Listing(game_id=games[1].id, owner_id=other_user.id, price=20, available=False),
        Listing(game_id=games[2].id, owner_id=renter.id, price=30),
    ]
    test_session.add_all(listings)
    test_session.commit()
    for listing in listings:
        test_session.refresh(listing)
    rentals = [
        Rental(listing_id=listings[0].id, customer_id=renter.id, confirmed=True),
        Rental(listing_id=listings[1].id, customer_id=renter.id, confirmed=True),
        Rental(listing_id=listings[2].id, customer_id=other_user.id),
    ]
    test_session.add_all(rentals)
    test_session.commit()
    for rental in rentals:
        test_session.refresh(rental)

    app.dependency_overrides[get_session] = lambda: test_session
    client = TestClient(app)
    token = create_access_token({"sub": str(renter.id), "role": renter.role})
    client.cookies.set("access_token", token)

    page = client.get("/rentals")
    assert page.status_code == 200
    assert "Return game" in page.text
    assert "Other person&#39;s rental" not in page.text
    form_return = client.post(
        f"/rentals/{rentals[0].id}/return", follow_redirects=False
    )
    assert form_return.status_code == 303
    api_return = client.post(f"/api/rentals/{rentals[1].id}/return")
    assert api_return.status_code == 200
    assert api_return.json()["return_requested_date"] is not None
    assert api_return.json()["return_date"] is None
    forbidden_return = client.post(f"/api/rentals/{rentals[2].id}/return")
    assert forbidden_return.status_code == 403

    test_session.refresh(rentals[0])
    test_session.refresh(rentals[1])
    test_session.refresh(listings[0])
    test_session.refresh(listings[1])
    assert rentals[0].return_requested_date is not None
    assert rentals[1].return_requested_date is not None
    assert rentals[0].return_date is None
    assert rentals[1].return_date is None
    assert not listings[0].available
    assert not listings[1].available
    assert rentals[2].return_date is None

    pending_page = client.get("/rentals")
    assert "Awaiting return confirmation" in pending_page.text
    assert "Return game" not in pending_page.text
    client.cookies.set(
        "access_token",
        create_access_token({"sub": str(other_user.id), "role": other_user.role}),
    )
    return_queue = client.get("/staff/returns")
    assert return_queue.status_code == 200
    assert "Return by form" in return_queue.text
    assert "Return by API" in return_queue.text
    assert "Other person&#39;s rental" not in return_queue.text
    for rental in rentals[:2]:
        confirmed = client.post(f"/api/rentals/{rental.id}/confirm-return")
        assert confirmed.status_code == 200
        assert confirmed.json()["return_date"] is not None

    for rental, listing in zip(rentals[:2], listings[:2]):
        test_session.refresh(rental)
        test_session.refresh(listing)
        assert rental.return_date is not None
        assert listing.available

    client.cookies.set("access_token", token)
    returned_page = client.get("/rentals")
    assert "Returned" in returned_page.text
    assert "Return game" not in returned_page.text
    app.dependency_overrides.clear()


def test_demo_rental_seed_is_idempotent(test_session):
    from app.cli import _seed_demo_rental

    customer = AuthService(UserRepository(test_session)).register_user(
        "seeded", "seeded@example.com", "secret"
    )
    game = Game(title="Mario Kart 8 Deluxe", platform="NSW", rating="E")
    test_session.add(game)
    test_session.commit()
    test_session.refresh(game)
    listing = Listing(game_id=game.id, owner_id=customer.id, price=39.99)
    test_session.add(listing)
    test_session.commit()

    assert _seed_demo_rental(test_session, customer)
    test_session.commit()
    assert not _seed_demo_rental(test_session, customer)
    test_session.commit()
    assert len(test_session.exec(select(Rental)).all()) == 1
    assert len(test_session.exec(select(Payment)).all()) == 1


def test_authenticated_user_can_add_and_view_their_listing(test_session):
    user = AuthService(UserRepository(test_session)).register_user(
        "seller", "seller@example.com", "secret"
    )
    app.dependency_overrides[get_session] = lambda: test_session
    client = TestClient(app)

    assert client.get("/listings/new").status_code == 401
    assert client.post(
        "/games",
        json={"title": "Anonymous Game", "platform": "PC"},
    ).status_code == 401
    token = create_access_token({"sub": str(user.id), "role": user.role})
    client.cookies.set("access_token", token)

    page = client.get("/listings/new")
    assert page.status_code == 200
    assert "Add a game for rent" in page.text

    game = client.post(
        "/games",
        json={
            "title": "Seller's Game",
            "rating": "E",
            "platform": "NSW",
            "genre": "Puzzle",
        },
    )
    assert game.status_code == 201
    listing = client.post(
        "/listings",
        json={
            "game_id": game.json()["id"],
            "condition": "good",
            "price": 18.50,
            "owner_id": 999,
        },
    )
    assert listing.status_code == 201
    assert listing.json()["owner_id"] == user.id

    other_user = AuthService(UserRepository(test_session)).register_user(
        "other-seller", "other-seller@example.com", "secret"
    )
    other_game = Game(title="Other Seller's Game", platform="PS5", rating="T")
    test_session.add(other_game)
    test_session.commit()
    test_session.refresh(other_game)
    test_session.add(Listing(game_id=other_game.id, owner_id=other_user.id, price=30))
    test_session.commit()

    mine = client.get("/my-listings")
    assert mine.status_code == 200
    assert "Seller&#39;s Game" in mine.text
    assert "Pending staff review" in mine.text
    assert "Other Seller&#39;s Game" not in mine.text
    assert "My Listings" in mine.text

    app.dependency_overrides.clear()


def test_staff_account_has_only_a_blank_home_page(test_session):
    staff = AuthService(UserRepository(test_session)).register_user(
        "staffer", "staffer@example.com", "secret"
    )
    staff.role = "staff"
    test_session.add(staff)
    test_session.commit()

    app.dependency_overrides[get_session] = lambda: test_session
    client = TestClient(app)
    login = client.post(
        "/login",
        data={"username": "staffer", "password": "secret"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.headers["location"].endswith("/app")

    home = client.get("/app")
    assert home.status_code == 200
    assert "Browse games" not in home.text
    assert "My Rentals" not in home.text
    assert "My Listings" not in home.text

    for path in ("/rentals", "/listings/new", "/my-listings"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].endswith("/app")

    app.dependency_overrides.clear()


def test_customer_listing_requires_staff_confirmation(test_session):
    customer = AuthService(UserRepository(test_session)).register_user(
        "requester", "requester@example.com", "secret"
    )
    staff = AuthService(UserRepository(test_session)).register_user(
        "approver", "approver@example.com", "secret"
    )
    staff.role = "staff"
    test_session.add(staff)
    test_session.commit()

    app.dependency_overrides[get_session] = lambda: test_session
    client = TestClient(app)
    customer_token = create_access_token(
        {"sub": str(customer.id), "role": customer.role}
    )
    client.cookies.set("access_token", customer_token)
    game = client.post(
        "/games",
        json={"title": "Awaiting Approval", "platform": "NSW", "rating": "E"},
    )
    listing = client.post(
        "/listings",
        json={"game_id": game.json()["id"], "condition": "good", "price": 16},
    )
    assert listing.status_code == 201
    assert listing.json()["confirmed"] is False

    public_listings = client.get("/listings", params={"platform": "NSW"}).json()
    assert all(row["id"] != listing.json()["id"] for row in public_listings)

    client.cookies.set(
        "access_token",
        create_access_token({"sub": str(staff.id), "role": staff.role}),
    )
    queue = client.get("/staff/listings")
    assert queue.status_code == 200
    assert "Awaiting Approval" in queue.text
    confirmed = client.post(f"/api/listings/{listing.json()['id']}/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["confirmed"] is True

    public_listings = client.get("/listings", params={"platform": "NSW"}).json()
    assert any(row["id"] == listing.json()["id"] for row in public_listings)
    app.dependency_overrides.clear()


def test_rental_requires_staff_confirmation_before_return(test_session):
    customer = AuthService(UserRepository(test_session)).register_user(
        "rental-requester", "rental-requester@example.com", "secret"
    )
    staff = AuthService(UserRepository(test_session)).register_user(
        "rental-staff", "rental-staff@example.com", "secret"
    )
    staff.role = "staff"
    test_session.add(staff)
    game = Game(title="Rental Approval Game", platform="PS5", rating="E")
    test_session.add(game)
    test_session.commit()
    test_session.refresh(game)
    listing = Listing(
        game_id=game.id,
        owner_id=customer.id,
        price=22,
        confirmed=True,
    )
    test_session.add(listing)
    test_session.commit()
    test_session.refresh(listing)

    app.dependency_overrides[get_session] = lambda: test_session
    client = TestClient(app)
    customer_token = create_access_token(
        {"sub": str(customer.id), "role": customer.role}
    )
    client.cookies.set("access_token", customer_token)
    created = client.post(
        "/rentals",
        json={"listing_id": listing.id, "customer_id": customer.id},
    )
    assert created.status_code == 201
    assert created.json()["confirmed"] is False
    rental_id = created.json()["id"]
    pending_return = client.post(f"/api/rentals/{rental_id}/return")
    assert pending_return.status_code == 409
    customer_rentals = client.get("/rentals")
    assert "Awaiting staff confirmation" in customer_rentals.text
    assert "Return game" not in customer_rentals.text
    assert client.get("/staff/rentals").status_code == 403
    customer_confirm = client.post(f"/api/rentals/{rental_id}/confirm")
    assert customer_confirm.status_code == 403

    client.cookies.set(
        "access_token",
        create_access_token({"sub": str(staff.id), "role": staff.role}),
    )
    queue = client.get("/staff/rentals")
    assert queue.status_code == 200
    assert "Rental Approval Game" in queue.text

    confirmed = client.post(f"/api/rentals/{rental_id}/confirm")
    assert confirmed.json()["confirmed"] is True
    empty_queue = client.get("/staff/rentals")
    assert "No rental requests" in empty_queue.text

    client.cookies.set("access_token", customer_token)
    return_request = client.post(f"/api/rentals/{rental_id}/return")
    assert return_request.status_code == 200
    assert return_request.json()["return_requested_date"] is not None
    assert return_request.json()["return_date"] is None

    client.cookies.set(
        "access_token",
        create_access_token({"sub": str(staff.id), "role": staff.role}),
    )
    return_queue = client.get("/staff/returns")
    assert "Rental Approval Game" in return_queue.text
    confirmed_return = client.post(f"/api/rentals/{rental_id}/confirm-return")
    assert confirmed_return.status_code == 200
    assert confirmed_return.json()["return_date"] is not None
    assert confirmed_return.json()["available"] is True
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("provider", "service_name", "checkout_url"),
    [
        ("stripe", "create_stripe_checkout", "https://checkout.stripe.test/session"),
        ("paypal", "create_paypal_checkout", "https://paypal.test/approve"),
    ],
)
def test_checkout_redirects_to_selected_provider(
    test_session, monkeypatch, provider, service_name, checkout_url
):
    user = AuthService(UserRepository(test_session)).register_user(
        "checkout", "checkout@example.com", "secret"
    )
    game = Game(title="Checkout Game", platform="PS5", rating="E")
    test_session.add(game)
    test_session.commit()
    test_session.refresh(game)
    listing = Listing(game_id=game.id, owner_id=user.id, price=12.50)
    test_session.add(listing)
    test_session.commit()
    test_session.refresh(listing)

    async def fake_checkout(settings, request, listing, user):
        return checkout_url

    monkeypatch.setattr(
        f"app.routers.marketplace.{service_name}", fake_checkout, raising=False
    )
    app.dependency_overrides[get_session] = lambda: test_session
    client = TestClient(app)
    token = create_access_token({"sub": str(user.id), "role": user.role})
    client.cookies.set("access_token", token)

    response = client.post(
        f"/checkout/{provider}", data={"listing_id": listing.id}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == checkout_url
    assert test_session.exec(select(Rental)).all() == []
    assert test_session.exec(select(Payment)).all() == []
    app.dependency_overrides.clear()


def test_verified_checkout_creates_payment_and_rental(test_session, monkeypatch):
    user = AuthService(UserRepository(test_session)).register_user(
        "paid", "paid@example.com", "secret"
    )
    game = Game(title="Paid Game", platform="NSW", rating="E")
    test_session.add(game)
    test_session.commit()
    test_session.refresh(game)
    listing = Listing(game_id=game.id, owner_id=user.id, price=17.99)
    test_session.add(listing)
    test_session.commit()
    test_session.refresh(listing)

    async def fake_verification(settings, session_id, user_id):
        return SimpleNamespace(
            user_id=user_id,
            listing_id=listing.id,
            amount=listing.price,
            transaction_id="stripe-payment-123",
        )

    monkeypatch.setattr(
        "app.routers.marketplace.verify_stripe_checkout", fake_verification, raising=False
    )
    app.dependency_overrides[get_session] = lambda: test_session
    client = TestClient(app)
    token = create_access_token({"sub": str(user.id), "role": user.role})
    client.cookies.set("access_token", token)

    response = client.get(
        "/payments/stripe/success?session_id=checkout-session-123",
        follow_redirects=False,
    )
    repeated_response = client.get(
        "/payments/stripe/success?session_id=checkout-session-123",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert repeated_response.status_code == 303
    payment = test_session.exec(select(Payment)).one()
    rental = test_session.exec(select(Rental)).one()
    test_session.refresh(listing)
    assert payment.customer_id == user.id
    assert payment.amount == listing.price
    assert rental.payment_id == payment.id
    assert rental.customer_id == user.id
    assert not listing.available
    app.dependency_overrides.clear()
