#!/usr/bin/env python3
"""FastStarter project CLI — stdlib argparse (no extra CLI library).

From the project root (venv active, deps installed; ``.env`` optional — falls back to ``.env.example``):

    python manage.py init
    python manage.py run
    python manage.py users
    python manage.py report --name "Student Name" --id "816000000"
    python manage.py usecase
    python manage.py skills-verify
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _ensure_models_loaded() -> None:
    import app.models  # noqa: F401


def _seed_demo_rental(
    session,
    customer,
    game_title: str = "Mario Kart 8 Deluxe",
    platform: str = "NSW",
) -> bool:
    from sqlmodel import select

    from app.models.domain import Game, Listing, Payment, Rental

    game = session.exec(
        select(Game).where(
            Game.title == game_title,
            Game.platform == platform,
        )
    ).first()
    if game is None or customer is None:
        return False

    listing = session.exec(
        select(Listing)
        .where(Listing.game_id == game.id)
        .order_by(Listing.id)
    ).first()
    if listing is None:
        return False

    existing_rental = session.exec(
        select(Rental).where(
            Rental.listing_id == listing.id,
            Rental.customer_id == customer.id,
        )
    ).first()
    if existing_rental:
        listing.available = False
        session.add(listing)
        return False
    if not listing.available:
        return False

    payment = Payment(customer_id=customer.id, amount=listing.price)
    session.add(payment)
    session.flush()
    session.add(
        Rental(
            listing_id=listing.id,
            customer_id=customer.id,
            payment_id=payment.id,
            confirmed=True,
        )
    )
    listing.available = False
    session.add(listing)
    return True


def cmd_init(args: argparse.Namespace) -> None:
    """Create database tables (drops existing by default) and seed demo users."""
    from app.config import get_settings
    from app.database import drop_all, ensure_db_and_tables

    _ensure_models_loaded()
    if args.drop:
        print("Dropping all tables…")
        # Drop can fail on a brand-new empty DB; create path still retries.
        try:
            drop_all()
        except Exception as exc:  # noqa: BLE001
            from app.database import is_db_not_ready_error

            if not is_db_not_ready_error(exc):
                raise
            print(f"Database not ready yet while dropping ({exc}); continuing…")
    print("Creating tables…")
    ensure_db_and_tables()
    print(f"Database ready ({get_settings().database_uri}).")
    if getattr(args, "seed", True):
        cmd_seed(args)


def cmd_seed(args: argparse.Namespace) -> None:
    """Insert demo users and marketplace content.

    bob / bobpass       (regular_user)
    admin / adminpass   (admin)
    """
    from sqlmodel import select

    from app.config import get_settings
    from app.database import ensure_db_and_tables, get_cli_session
    from app.models.domain import Game, Listing, Staff
    from app.models.user import User, UserBase
    from app.repositories.user import UserRepository
    from app.schemas.user import AdminCreate, RegularUserCreate
    from app.utilities.security import encrypt_password

    _ensure_models_loaded()
    ensure_db_and_tables()

    demo_users = [
        ("bob", "bob@example.com", "bobpass", "regular_user"),
        ("admin", "admin@example.com", "adminpass", "admin"),
    ]
    if get_settings().env.lower() not in {"prod", "production"}:
        demo_users.append(("staff", "staff@example.com", "staffpass", "staff"))

    created = 0
    skipped = 0
    games_created = 0
    listings_created = 0
    rentals_created = 0
    with get_cli_session() as session:
        repo = UserRepository(session)
        for username, email, password, role in demo_users:
            if repo.get_by_username(username):
                print(f"  skip  {username} (already exists)")
                skipped += 1
                continue
            if role == "admin":
                payload_cls = AdminCreate
            elif role == "staff":
                payload_cls = UserBase
            else:
                payload_cls = RegularUserCreate
            repo.create(
                payload_cls(
                    username=username,
                    email=email,
                    password=encrypt_password(password),
                    role=role,
                )
            )
            print(f"  create {username} ({role})")
            created += 1

        staff_user = repo.get_by_username("staff")
        if staff_user and not session.exec(
            select(Staff).where(Staff.user_id == staff_user.id)
        ).first():
            session.add(Staff(user_id=staff_user.id))

        owner = session.exec(select(User).where(User.username == "bob")).first()
        sample_games = [
            ("Mario Kart 8 Deluxe", "E", "NSW", "Racing", "excellent", 39.99),
            ("The Legend of Zelda: Tears of the Kingdom", "E10+", "NSW", "Adventure", "good", 49.99),
            ("Horizon Forbidden West", "T", "PS5", "Action", "good", 34.99),
            ("Super Smash Bros. Ultimate", "E10+", "NSW", "Fighting", "excellent", 44.99),
            ("Elden Ring", "M", "PS5", "Action RPG", "good", 32.99),
            ("Minecraft", "E10+", "PC", "Sandbox", "good", 19.99),
        ]
        for title, rating, platform, genre, condition, price in sample_games:
            game = session.exec(
                select(Game).where(Game.title == title, Game.platform == platform)
            ).first()
            if game is None:
                game = Game(
                    title=title,
                    rating=rating,
                    platform=platform,
                    genre=genre,
                )
                session.add(game)
                session.flush()
                games_created += 1

            existing_listing = session.exec(
                select(Listing).where(
                    Listing.game_id == game.id,
                    Listing.owner_id == (owner.id if owner else None),
                    Listing.condition == condition,
                    Listing.price == price,
                )
            ).first()
            if existing_listing is None:
                session.add(
                    Listing(
                        game_id=game.id,
                        owner_id=owner.id if owner else None,
                        condition=condition,
                        price=price,
                        confirmed=True,
                    )
                )
                listings_created += 1
        sample_rentals = [
            ("Mario Kart 8 Deluxe", "NSW"),
            ("The Legend of Zelda: Tears of the Kingdom", "NSW"),
        ]
        rentals_created = sum(
            _seed_demo_rental(session, owner, game_title, platform)
            for game_title, platform in sample_rentals
        )
        session.commit()

    print(f"Seed done — created {created}, skipped {skipped}.")
    print(f"Marketplace seed — created {games_created} games, {listings_created} listings.")
    print(f"Rental seed — created {rentals_created} paid rental(s).")
    print("Login with bob/bobpass or admin/adminpass")


def cmd_run(args: argparse.Namespace) -> None:
    """Start the FastAPI app with Uvicorn."""
    import uvicorn

    from app.config import get_settings

    settings = get_settings()
    bind_host = args.host or settings.app_host
    bind_port = args.port or settings.app_port
    if args.reload is None:
        use_reload = settings.env.lower() != "production"
    else:
        use_reload = args.reload
    browser_host = "127.0.0.1" if bind_host in {"0.0.0.0", "::"} else bind_host
    print(f"Starting FastStarter on http://{browser_host}:{bind_port} (reload={use_reload})")
    uvicorn.run(
        "app.main:app",
        host=bind_host,
        port=bind_port,
        reload=use_reload,
    )


def cmd_report(args: argparse.Namespace) -> None:
    """Export docs/report.md to PDF; merges judge + exports Guide transcripts."""
    from app.report_pdf import export_report
    from app.skill_integrity import format_report, verify

    export_report(
        name=args.name,
        student_id=args.student_id,
        source=None if args.src is None else Path(args.src),
        output=None if args.output is None else Path(args.output),
    )
    print(format_report(verify()))


def cmd_transcripts(args: argparse.Namespace) -> None:
    """Dump all native Guide project transcripts into docs/transcripts/ for submission."""
    from app.transcript_export import export_project_transcripts

    result = export_project_transcripts(make_zip=not args.no_zip)
    if result.found == 0:
        print(
            "Warning: no Guide transcripts found. "
            "Set FASTSTARTER_TRANSCRIPTS_DIR if chats live outside Cursor's agent-transcripts folder."
        )
        raise SystemExit(2)
    print(f"Submission dump ready: {result.out_dir}")
    if result.zip_path:
        print(f"Zip for submission: {result.zip_path}")


def cmd_skills_verify(args: argparse.Namespace) -> None:
    """Check course skill files against .agents/skills.lock.json."""
    from app.skill_integrity import format_report, verify

    result = verify()
    print(format_report(result))
    if not result.ok:
        raise SystemExit(1)


def cmd_usecase(args: argparse.Namespace) -> None:
    """Render docs/diagrams/use-case.json to a UML use-case PNG."""
    from app.usecase_diagram import render_usecase_png

    dest = render_usecase_png(
        spec_path=None if args.spec is None else Path(args.spec),
        output=None if args.output is None else Path(args.output),
    )
    print(f"Wrote {dest}")


def cmd_skills_lock(args: argparse.Namespace) -> None:
    """Rewrite the skill lockfile (course authors only)."""
    from app.skill_integrity import write_lock

    dest = write_lock()
    print(f"Wrote {dest}")


def cmd_users(args: argparse.Namespace) -> None:
    """List users currently in the database."""
    from sqlmodel import select

    from app.database import get_cli_session
    from app.models.user import User

    _ensure_models_loaded()
    with get_cli_session() as session:
        users = session.exec(select(User)).all()
        if not users:
            print("No users found. Run: python manage.py init")
            return
        for user in users:
            print(
                f"  id={user.id}  username={user.username}  "
                f"role={user.role}  email={user.email}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python manage.py",
        description="FastStarter Python CLI — init database, seed demo data, run the app.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser(
        "init",
        help="Create DB tables and seed demo users (drops existing tables by default)",
    )
    p_init.add_argument(
        "--no-drop",
        dest="drop",
        action="store_false",
        help="Create tables without dropping existing ones",
    )
    p_init.add_argument(
        "--no-seed",
        dest="seed",
        action="store_false",
        help="Skip demo user seed after creating tables",
    )
    p_init.set_defaults(drop=True, seed=True, func=cmd_init)

    p_seed = sub.add_parser(
        "seed",
        help="Insert demo users and marketplace content (idempotent)",
    )
    p_seed.set_defaults(func=cmd_seed)

    p_run = sub.add_parser("run", help="Start the web app (uvicorn)")
    p_run.add_argument("--host", default=None, help="Bind host")
    p_run.add_argument("--port", type=int, default=None, help="Bind port")
    reload_group = p_run.add_mutually_exclusive_group()
    reload_group.add_argument(
        "--reload", dest="reload", action="store_true", default=None, help="Enable auto-reload"
    )
    reload_group.add_argument(
        "--no-reload", dest="reload", action="store_false", help="Disable auto-reload"
    )
    p_run.set_defaults(func=cmd_run, reload=None)

    p_users = sub.add_parser("users", help="List users in the database")
    p_users.set_defaults(func=cmd_users)

    p_report = sub.add_parser(
        "report",
        help="Export docs/report.md to PDF; merges judge.md and exports Guide transcripts",
    )
    p_report.add_argument("--name", required=True, help="Student name (printed on the PDF cover)")
    p_report.add_argument("--id", dest="student_id", required=True, help="Student ID (PDF only)")
    p_report.add_argument("--src", default=None, help="Markdown path (default: docs/report.md)")
    p_report.add_argument("--output", default=None, help="PDF path (default: docs/report.pdf)")
    p_report.set_defaults(func=cmd_report)

    p_transcripts = sub.add_parser(
        "transcripts",
        help="Dump all native Guide project chats to docs/transcripts/ (+ zip) for submission",
    )
    p_transcripts.add_argument(
        "--no-zip",
        action="store_true",
        help="Skip writing docs/transcripts.zip",
    )
    p_transcripts.set_defaults(func=cmd_transcripts)

    p_usecase = sub.add_parser(
        "usecase",
        help="Render docs/diagrams/use-case.json to a UML use-case PNG",
    )
    p_usecase.add_argument("--spec", default=None, help="JSON spec (default: docs/diagrams/use-case.json)")
    p_usecase.add_argument("--output", default=None, help="PNG path (default: docs/diagrams/use-case.png)")
    p_usecase.set_defaults(func=cmd_usecase)

    p_skills_verify = sub.add_parser(
        "skills-verify",
        help="Check course skills against .agents/skills.lock.json",
    )
    p_skills_verify.set_defaults(func=cmd_skills_verify)

    p_skills_lock = sub.add_parser(
        "skills-lock",
        help="Rewrite .agents/skills.lock.json (course authors; needs FASTSTARTER_SKILLS_LOCK=1)",
    )
    p_skills_lock.set_defaults(func=cmd_skills_lock)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
