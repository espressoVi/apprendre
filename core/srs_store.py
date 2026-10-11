import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from core.segmentation import normalize_key
from core.srs_scheduler import SRSSchedulingMixin


class SRSStore(SRSSchedulingMixin):
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS lexical_cache (
                    lookup_key TEXT PRIMARY KEY,
                    data_json TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS cards (
                    id TEXT PRIMARY KEY,
                    language TEXT NOT NULL,
                    lookup_key TEXT NOT NULL,
                    canonical TEXT NOT NULL,
                    pos TEXT NOT NULL,
                    surface TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    article_id TEXT NOT NULL,
                    context TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'new',
                    created_at REAL NOT NULL,
                    UNIQUE(language, lookup_key)
                )
            """)
            columns = { row["name"] for row in db.execute("PRAGMA table_info(cards)") }
            if not {"lookup_key", "data_json"} <= columns:
                raise RuntimeError( "Old flashcard schema detected. Stop the application. ")
        self._initialize_srs()


    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row

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

    def saved_keys(self):
        with self.connection() as db:
            rows = db.execute(
                "SELECT lookup_key FROM cards WHERE language = 'fr'"
            ).fetchall()

        return {row["lookup_key"] for row in rows}

    def add(self, segment, article_id, context, lookup):
        with self.connection() as db:
            cursor = db.execute(
                """
                INSERT INTO cards (
                    id, language, lookup_key, canonical, pos,
                    surface, kind, article_id, context,
                    data_json, state, created_at
                )
                VALUES (?, 'fr', ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
                ON CONFLICT(language, lookup_key) DO NOTHING
                """,
                (
                    str(uuid.uuid4()),
                    lookup["lookup_key"],
                    normalize_key(segment["canonical"]),
                    segment["pos"] or "UNKNOWN",
                    segment["text"],
                    segment["kind"],
                    article_id,
                    context,
                    json.dumps(lookup["data"], ensure_ascii=False),
                    time.time(),
                ),
            )
            created = cursor.rowcount == 1
            saved = db.execute(
                """
                SELECT id FROM cards
                WHERE language = 'fr' AND lookup_key = ?
                """,
                (lookup["lookup_key"],),
            ).fetchone()
            self._ensure_scheduled(db, saved["id"])
            row = db.execute(
                """
                SELECT * FROM cards
                WHERE language = 'fr' AND lookup_key = ?
                """,
                (lookup["lookup_key"],),
            ).fetchone()
        return created, self._decode_card(row)

    @staticmethod
    def _decode_card(row):
        card = dict(row)
        card["data"] = json.loads(card.pop("data_json"))
        card.pop("fsrs_json", None)
        return card

    def list_cards(self):
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM cards ORDER BY created_at DESC, id"
            ).fetchall()

        return [self._decode_card(row) for row in rows]
