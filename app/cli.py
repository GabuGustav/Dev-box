import argparse

from app.database import SessionLocal
from app.models import APIKey, Principal
from app.security import generate_api_key, hash_api_key


def create_principal(principal_type: str, name: str) -> None:
    raw_key = generate_api_key()
    with SessionLocal() as db:
        principal = Principal(name=name, principal_type=principal_type)
        db.add(principal)
        db.flush()
        db.add(
            APIKey(
                principal_id=principal.id,
                key_hash=hash_api_key(raw_key),
                key_prefix=raw_key[:16],
            )
        )
        db.commit()
        print(f"Principal ID: {principal.id}")
        print(f"Principal type: {principal.principal_type}")
        print(f"API key (shown once): {raw_key}")


def main() -> None:
    parser = argparse.ArgumentParser(description="DevBox administration commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create-principal")
    create.add_argument("--type", choices=["user", "agent"], required=True)
    create.add_argument("--name", required=True)
    args = parser.parse_args()
    if args.command == "create-principal":
        create_principal(args.type, args.name)


if __name__ == "__main__":
    main()
