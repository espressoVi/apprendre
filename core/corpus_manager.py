import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import spacy
from datasketch import MinHash, MinHashLSH


class CorpusManager:
    MIGRATION_KEY = "corpus-jsonl-v1"

    def __init__(
        self,
        filepath="data/corpus.sqlite3",
        threshold=0.7,
        nlp=None,
        legacy_path=None,
    ):
        self.filepath = Path(filepath)

        if self.filepath.suffix.lower() in {".jsonl", ".json"}:
            raise ValueError(
                "filepath must point to SQLite; use legacy_path for JSONL"
            )

        self.filepath.parent.mkdir(parents=True, exist_ok=True)

        self.nlp = (
            nlp
            if nlp is not None
            else spacy.load("fr_core_news_sm", disable=["ner", "parser"])
        )

        if "sentencizer" not in self.nlp.pipe_names:
            self.nlp.add_pipe("sentencizer")

        self.lsh = MinHashLSH(threshold=threshold, num_perm=128)
        self._indexed_ids = set()

        self._create_schema()

        if legacy_path is not None:
            self.migrate_jsonl(legacy_path)

        with self.connection() as db:
            self._sync_index(db)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.filepath, timeout=30)
        db.row_factory = sqlite3.Row

        try:
            with db:
                yield db
        finally:
            db.close()

    def _create_schema(self):
        with self.connection() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS articles (
                    unique_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    text TEXT NOT NULL,
                    read_status INTEGER NOT NULL DEFAULT 0
                        CHECK (read_status IN (0, 1)),
                    timestamp REAL NOT NULL
                )
            """)

            db.execute("""
                CREATE INDEX IF NOT EXISTS articles_read_timestamp
                ON articles (read_status, timestamp DESC)
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS corpus_migrations (
                    name TEXT PRIMARY KEY,
                    completed_at REAL NOT NULL
                )
            """)

    @staticmethod
    def _validate_legacy_document(document):
        if not isinstance(document, dict):
            raise ValueError("Expected a JSON object")

        for field in ("unique_id", "title", "text"):
            if not isinstance(document.get(field), str):
                raise ValueError(f"{field} must be a string")

        if not document["unique_id"]:
            raise ValueError("unique_id cannot be empty")

        read_status = document.get("read_status", False)
        if not isinstance(read_status, bool):
            raise ValueError("read_status must be a boolean")

        timestamp = document.get("timestamp", 0)
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(timestamp)
        ):
            raise ValueError("timestamp must be a finite number")

        return (
            document["unique_id"],
            document["title"],
            document["text"],
            int(read_status),
            timestamp,
        )

    def migrate_jsonl(self, source_path):
        """Import once, without modifying the legacy file.

        A malformed record, duplicate ID, or database error rolls back
        all records imported by this attempt and its completion marker.
        """
        source = Path(source_path)

        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")

            completed = db.execute(
                "SELECT 1 FROM corpus_migrations WHERE name = ?",
                (self.MIGRATION_KEY,),
            ).fetchone()

            if completed:
                return 0

            if not source.exists():
                # Do not mark complete: a source supplied later can be imported.
                return 0

            imported = 0

            with source.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        continue

                    try:
                        values = self._validate_legacy_document(
                            json.loads(line)
                        )

                        db.execute(
                            """
                            INSERT INTO articles (
                                unique_id, title, text,
                                read_status, timestamp
                            )
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            values,
                        )
                    except (
                        ValueError,
                        TypeError,
                        OverflowError,
                        sqlite3.IntegrityError,
                    ) as exc:
                        raise ValueError(
                            f"{source}:{line_number}: {exc}"
                        ) from exc

                    imported += 1

            db.execute(
                """
                INSERT INTO corpus_migrations (name, completed_at)
                VALUES (?, ?)
                """,
                (self.MIGRATION_KEY, time.time()),
            )

        return imported

    def _sync_index(self, db):
        """Index newly persisted articles, including other workers' inserts.

        Articles are append-only: read-status changes don't affect MinHash.
        """
        rows = db.execute(
            "SELECT unique_id, text FROM articles"
        )

        for row in rows:
            doc_id = row["unique_id"]

            if doc_id in self._indexed_ids:
                continue

            signature = self._compute_minhash(row["text"])
            self.lsh.insert(doc_id, signature)
            self._indexed_ids.add(doc_id)

    def _compute_minhash(
        self,
        text: str,
        num_sentences: int = 3,
    ) -> MinHash:
        doc = self.nlp(text)
        sentences = list(doc.sents)[:num_sentences]

        signature = MinHash(num_perm=128)

        for sentence in sentences:
            for token in sentence:
                if (
                    not token.is_stop
                    and not token.is_punct
                    and not token.is_space
                ):
                    lemma = token.lemma_ or token.text
                    signature.update(lemma.lower().encode("utf-8"))

        return signature

    def add_document(self, title: str, text: str) -> tuple[bool, str]:
        signature = self._compute_minhash(text)
        doc_id = str(uuid.uuid4())

        with self.connection() as db:
            # Serialize duplicate-check-and-insert across processes.
            db.execute("BEGIN IMMEDIATE")
            self._sync_index(db)

            duplicates = self.lsh.query(signature)

            if duplicates:
                return (
                    False,
                    f"Duplicate found mapping to ID: {duplicates[0]}",
                )

            db.execute(
                """
                INSERT INTO articles (
                    unique_id, title, text, read_status, timestamp
                )
                VALUES (?, ?, ?, 0, ?)
                """,
                (doc_id, title, text, time.time()),
            )

        # Update the in-memory index only after the database commit.
        self.lsh.insert(doc_id, signature)
        self._indexed_ids.add(doc_id)

        return True, doc_id

    def get_articles_metadata(self):
        with self.connection() as db:
            rows = db.execute("""
                SELECT
                    unique_id AS id,
                    title,
                    read_status,
                    timestamp
                FROM articles
                ORDER BY read_status ASC, timestamp DESC, unique_id
            """).fetchall()

        return [
            {
                **dict(row),
                "read_status": bool(row["read_status"]),
            }
            for row in rows
        ]

    def get_article(self, doc_id: str):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM articles WHERE unique_id = ?",
                (doc_id,),
            ).fetchone()

        if row is None:
            return None

        return {
            **dict(row),
            "read_status": bool(row["read_status"]),
        }

    def mark_as_read(self, doc_id: str):
        with self.connection() as db:
            cursor = db.execute(
                """
                UPDATE articles
                SET read_status = 1
                WHERE unique_id = ?
                """,
                (doc_id,),
            )

            return cursor.rowcount == 1
