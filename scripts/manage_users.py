import argparse
import getpass
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from core.auth import DATA_DIR, get_auth
from core.shared_cache import SharedLexicalCache


def backup_database(source: Path, destination: Path):
    source_uri = source.resolve().as_uri() + "?mode=ro"

    with sqlite3.connect(source_uri, uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)


def import_legacy(username: str, source: Path):
    user = get_auth().user_by_name(username)
    if not user:
        raise ValueError("Create the user first")

    source = source.resolve()
    parent = DATA_DIR / "users"
    parent.mkdir(parents=True, exist_ok=True)

    destination = parent / user["id"]
    if destination.exists():
        raise ValueError(
            "User storage already exists. Import before first login."
        )

    corpus = source / "corpus.sqlite3"
    srs = source / "srs.sqlite3"
    legacy_jsonl = source / "corpus.jsonl"

    if not corpus.exists() and legacy_jsonl.exists():
        raise ValueError(
            "Only corpus.jsonl exists. First run the old application "
            "once to migrate it to corpus.sqlite3."
        )

    if not corpus.exists() and not srs.exists():
        raise ValueError("No legacy databases found")

    stage = Path(tempfile.mkdtemp(
        prefix=".import-",
        dir=parent,
    ))

    try:
        if corpus.exists():
            backup_database(corpus, stage / "corpus.sqlite3")

        if srs.exists():
            backup_database(srs, stage / "srs.sqlite3")

            shared = SharedLexicalCache(
                DATA_DIR / "shared" / "lexical.sqlite3"
            )

            uri = srs.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as src:
                exists = src.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'lexical_cache'
                    """
                ).fetchone()

                if exists:
                    with shared.connection() as dst:
                        for row in src.execute(
                            """
                            SELECT lookup_key, data_json, model, created_at
                            FROM lexical_cache
                            """
                        ):
                            dst.execute(
                                """
                                INSERT INTO lexical_cache (
                                    lookup_key, data_json, model, created_at
                                )
                                VALUES (?, ?, ?, ?)
                                ON CONFLICT(lookup_key) DO NOTHING
                                """,
                                row,
                            )

        os.rename(stage, destination)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    print(f"Imported legacy progress for {user['username']}")


def password_prompt():
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")

    if password != confirmation:
        raise ValueError("Passwords do not match")

    return password


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    create.add_argument("username")

    password = commands.add_parser("password")
    password.add_argument("username")

    commands.add_parser("list")

    legacy = commands.add_parser("import-legacy")
    legacy.add_argument("username")
    legacy.add_argument("--source", type=Path, required=True)

    args = parser.parse_args()

    if args.command == "create":
        user = get_auth().create_user(
            args.username,
            password_prompt(),
        )
        print(f"Created {user['username']} ({user['id']})")

    elif args.command == "password":
        get_auth().set_password(
            args.username,
            password_prompt(),
        )
        print("Password changed; existing sessions revoked")

    elif args.command == "list":
        for user in get_auth().list_users():
            print(f"{user['username']}\t{user['id']}")

    elif args.command == "import-legacy":
        import_legacy(args.username, args.source)


if __name__ == "__main__":
    main()
