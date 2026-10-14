import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class SharedLexicalCache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS lexical_cache (
                    lookup_key TEXT PRIMARY KEY,
                    data_json TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")

        try:
            with db:
                yield db
        finally:
            db.close()

    def get_cached(self, key):
        with self.connection() as db:
            row = db.execute(
                """
                SELECT data_json FROM lexical_cache
                WHERE lookup_key = ?
                """,
                (key,),
            ).fetchone()

        return row["data_json"] if row else None

    def delete_cached(self, key):
        with self.connection() as db:
            db.execute(
                "DELETE FROM lexical_cache WHERE lookup_key = ?",
                (key,),
            )

    def put_cached(self, key, data_json, model):
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO lexical_cache (
                    lookup_key, data_json, model, created_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lookup_key) DO NOTHING
                """,
                (key, data_json, model, time.time()),
            )
            row = db.execute(
                """
                SELECT data_json FROM lexical_cache
                WHERE lookup_key = ?
                """,
                (key,),
            ).fetchone()

        return row["data_json"]
