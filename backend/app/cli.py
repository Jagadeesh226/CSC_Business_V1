"""Backend management commands.

Create the first admin (there is no way to sign in before one exists):

    uv run python -m app.cli create-admin --name "Owner" --email owner@example.com

The password is prompted for and never echoed or passed on the command line.
"""

import argparse
import getpass
import sys

from pydantic import EmailStr, TypeAdapter, ValidationError

from app.db.session import SessionLocal, get_engine
from app.schemas.user import Password, UserName
from app.services import user_service
from app.services.errors import ServiceError


def _create_admin(args: argparse.Namespace) -> int:
    try:
        name = TypeAdapter(UserName).validate_python(args.name)
        # Same email rules as the API, so the admin can actually sign in.
        email = str(TypeAdapter(EmailStr).validate_python(args.email.strip()))
        password = getpass.getpass("Password (10-128 characters): ")
        if password != getpass.getpass("Repeat password: "):
            print("Passwords do not match.", file=sys.stderr)
            return 1
        TypeAdapter(Password).validate_python(password)
    except ValidationError as exc:
        print(f"Invalid input: {exc.errors()[0]['msg']}", file=sys.stderr)
        return 1

    with SessionLocal(bind=get_engine()) as db:
        try:
            user = user_service.create_admin(
                db, name=name, email=email, password=password
            )
            # Read while the session is open: commit expires the attributes.
            user_id, user_email = user.id, user.email
        except ServiceError as exc:
            print(exc.detail, file=sys.stderr)
            return 1
    print(f"Created admin user id={user_id} ({user_email}).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-admin", help="Create an admin user")
    create.add_argument("--name", required=True)
    create.add_argument("--email", required=True)
    args = parser.parse_args(argv)
    return _create_admin(args)


if __name__ == "__main__":
    raise SystemExit(main())
