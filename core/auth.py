import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(
    os.environ.get("APP_DATA_DIR", str(BASE_DIR / "data"))
).resolve()

COOKIE_NAME = "apprendre_session"
SESSION_SECONDS = 30 * 24 * 60 * 60
PASSWORD_ITERATIONS = 600_000

USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 1024:
        raise ValueError("Password must contain 12–1024 characters")

    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        PASSWORD_ITERATIONS,
    ).hex()

    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    if len(password) > 1024:
        return False

    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False

        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt),
            int(iterations),
        ).hex()

        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                        ON DELETE CASCADE
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS sessions_expiry
                ON sessions(expires_at)
            """)

        # Used for roughly comparable work when a username is unknown.
        self.dummy_hash = hash_password(secrets.token_urlsafe(32))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")

        try:
            with db:
                yield db
        finally:
            db.close()

    def create_user(self, username: str, password: str) -> dict:
        username = username.strip().lower()

        if not USERNAME_PATTERN.fullmatch(username):
            raise ValueError(
                "Username must be 1–64 lowercase letters, digits, "
                "dots, underscores, or hyphens"
            )

        user = {
            "id": uuid.uuid4().hex,
            "username": username,
        }
        password_hash = hash_password(password)

        with self.connection() as db:
            db.execute(
                """
                INSERT INTO users (
                    id, username, password_hash, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    user["id"],
                    username,
                    password_hash,
                    time.time(),
                ),
            )

        return user

    def user_by_name(self, username: str):
        with self.connection() as db:
            row = db.execute(
                "SELECT id, username FROM users WHERE username = ?",
                (username.strip().lower(),),
            ).fetchone()

        return dict(row) if row else None

    def list_users(self):
        with self.connection() as db:
            rows = db.execute(
                "SELECT id, username FROM users ORDER BY username"
            ).fetchall()

        return [dict(row) for row in rows]

    def login(self, username: str, password: str):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM users WHERE username = ?",
                (username.strip().lower(),),
            ).fetchone()

        valid = verify_password(
            password,
            row["password_hash"] if row else self.dummy_hash,
        )

        if not row or not valid:
            return None

        token = secrets.token_urlsafe(32)
        now = time.time()

        with self.connection() as db:
            db.execute(
                "DELETE FROM sessions WHERE expires_at <= ?",
                (now,),
            )
            db.execute(
                """
                INSERT INTO sessions (
                    token_hash, user_id, expires_at
                ) VALUES (?, ?, ?)
                """,
                (
                    token_hash(token),
                    row["id"],
                    now + SESSION_SECONDS,
                ),
            )

        return token

    def authenticate(self, token: str | None):
        if not token or len(token) > 256:
            return None

        with self.connection() as db:
            row = db.execute(
                """
                SELECT users.id, users.username
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ?
                  AND sessions.expires_at > ?
                """,
                (token_hash(token), time.time()),
            ).fetchone()

        return dict(row) if row else None

    def logout(self, token: str | None):
        if not token:
            return

        with self.connection() as db:
            db.execute(
                "DELETE FROM sessions WHERE token_hash = ?",
                (token_hash(token),),
            )

    def set_password(self, username: str, password: str):
        encoded = hash_password(password)

        with self.connection() as db:
            user = db.execute(
                "SELECT id FROM users WHERE username = ?",
                (username.strip().lower(),),
            ).fetchone()

            if not user:
                raise ValueError("User not found")

            db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (encoded, user["id"]),
            )
            db.execute(
                "DELETE FROM sessions WHERE user_id = ?",
                (user["id"],),
            )


@lru_cache(maxsize=1)
def get_auth() -> AuthStore:
    return AuthStore(DATA_DIR / "auth.sqlite3")
