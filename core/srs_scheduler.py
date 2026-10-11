import hashlib
import json
from datetime import datetime, timedelta, timezone

from fsrs import Card, Rating, Scheduler, State


RATINGS = {
    "again": Rating.Again,
    "hard": Rating.Hard,
    "good": Rating.Good,
    "easy": Rating.Easy,
}


class ReviewConflict(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc)


class SRSSchedulingMixin:
    def _initialize_srs(self):
        """Add scheduling fields without deleting cards or lexical data."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")

            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(cards)")
            }

            additions = {
                "fsrs_json": "TEXT",
                "due_at": "REAL",
                "review_count": "INTEGER NOT NULL DEFAULT 0",
                "lapses": "INTEGER NOT NULL DEFAULT 0",
                "version": "INTEGER NOT NULL DEFAULT 0",
            }

            for name, definition in additions.items():
                if name not in columns:
                    db.execute(
                        f"ALTER TABLE cards ADD COLUMN {name} {definition}"
                    )

            db.execute("""
                CREATE TABLE IF NOT EXISTS srs_settings (
                    name TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS srs_reviews (
                    request_id TEXT PRIMARY KEY,
                    card_id TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    rating TEXT NOT NULL,
                    reviewed_at REAL NOT NULL,
                    review_log_json TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL,
                    scheduler_json TEXT NOT NULL,
                    result_json TEXT NOT NULL
                )
            """)

            db.execute("""
                CREATE INDEX IF NOT EXISTS cards_due_idx
                ON cards (due_at, state)
            """)

            db.execute("""
                CREATE INDEX IF NOT EXISTS srs_reviews_card_idx
                ON srs_reviews (card_id, reviewed_at)
            """)

            settings = db.execute(
                "SELECT value_json FROM srs_settings WHERE name = 'scheduler'"
            ).fetchone()

            if settings:
                self.scheduler = Scheduler.from_json(
                    settings["value_json"]
                )
            else:
                self.scheduler = Scheduler(
                    desired_retention=0.9,
                    learning_steps=(
                        timedelta(minutes=1),
                        timedelta(minutes=10),
                    ),
                    relearning_steps=(timedelta(minutes=10),),
                    maximum_interval=36500,
                    # Makes interval previews agree with submitted reviews
                    # when reviewed at the same time.
                    enable_fuzzing=False,
                )

                db.execute(
                    """
                    INSERT INTO srs_settings (name, value_json)
                    VALUES ('scheduler', ?)
                    """,
                    (self.scheduler.to_json(),),
                )

            self.scheduler_json = self.scheduler.to_json()

            rows = db.execute(
                "SELECT id FROM cards WHERE fsrs_json IS NULL"
            ).fetchall()

            for row in rows:
                self._ensure_scheduled(db, row["id"])

    def _ensure_scheduled(self, db, card_id):
        row = db.execute(
            "SELECT * FROM cards WHERE id = ?",
            (card_id,),
        ).fetchone()

        if row is None or row["fsrs_json"] is not None:
            return

        # Stable, bounded integer identity for FSRS.
        digest = hashlib.sha256(card_id.encode("utf-8")).digest()
        fsrs_id = int.from_bytes(digest[:7], "big")

        created = datetime.fromtimestamp(
            row["created_at"], timezone.utc
        )
        card = Card(card_id=fsrs_id, due=created)

        db.execute(
            """
            UPDATE cards
            SET fsrs_json = ?, due_at = ?, state = 'new'
            WHERE id = ? AND fsrs_json IS NULL
            """,
            (card.to_json(), card.due.timestamp(), card_id),
        )

    @staticmethod
    def _summary(row):
        return {
            "id": row["id"],
            "lookup_key": row["lookup_key"],
            "state": row["state"],
            "due_at": row["due_at"],
            "version": row["version"],
            "review_count": row["review_count"],
            "lapses": row["lapses"],
        }

    def states_by_key(self):
        with self.connection() as db:
            rows = db.execute("""
                SELECT
                    id, lookup_key, state, due_at,
                    version, review_count, lapses
                FROM cards
                WHERE language = 'fr'
            """).fetchall()

        return {
            row["lookup_key"]: self._summary(row)
            for row in rows
        }

    def review_queue(self, limit=50, now=None):
        now = now or utc_now()
        timestamp = now.timestamp()

        with self.connection() as db:
            # Keep cards, counts and next_due in one read snapshot.
            db.execute("BEGIN")

            rows = db.execute(
                """
                SELECT * FROM cards
                WHERE due_at <= ?
                ORDER BY
                    CASE state
                        WHEN 'learning' THEN 0
                        WHEN 'learnt' THEN 1
                        ELSE 2
                    END,
                    due_at,
                    created_at,
                    id
                LIMIT ?
                """,
                (timestamp, limit),
            ).fetchall()

            totals = {"new": 0, "learning": 0, "learnt": 0}
            due_counts = {"new": 0, "learning": 0, "learnt": 0}

            for row in db.execute(
                "SELECT state, COUNT(*) AS count FROM cards GROUP BY state"
            ):
                totals[row["state"]] = row["count"]

            for row in db.execute(
                """
                SELECT state, COUNT(*) AS count
                FROM cards
                WHERE due_at <= ?
                GROUP BY state
                """,
                (timestamp,),
            ):
                due_counts[row["state"]] = row["count"]

            next_row = db.execute(
                "SELECT MIN(due_at) AS next_due FROM cards WHERE due_at > ?",
                (timestamp,),
            ).fetchone()

        cards = []

        for row in rows:
            card = self._decode_card(row)
            card["front"] = card["surface"]

            fsrs_card = Card.from_json(row["fsrs_json"])
            previews = {}

            for name, rating in RATINGS.items():
                predicted, _ = self.scheduler.review_card(
                    fsrs_card,
                    rating,
                    review_datetime=now,
                )
                previews[name] = max(
                    0,
                    (predicted.due - now).total_seconds(),
                )

            card["intervals"] = previews
            cards.append(card)

        return {
            "cards": cards,
            "counts": totals,
            "due_counts": due_counts,
            "due_total": sum(due_counts.values()),
            "next_due": next_row["next_due"],
            "server_now": timestamp,
        }

    def review(
        self,
        card_id,
        rating,
        expected_version,
        request_id,
        now=None,
    ):
        if rating not in RATINGS:
            raise ValueError("Invalid review rating")

        now = now or utc_now()

        if now.tzinfo != timezone.utc:
            raise ValueError("Review time must use UTC")

        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")

            previous_request = db.execute(
                "SELECT * FROM srs_reviews WHERE request_id = ?",
                (request_id,),
            ).fetchone()

            if previous_request:
                if (
                    previous_request["card_id"] != card_id
                    or previous_request["rating"] != rating
                    or previous_request["expected_version"]
                    != expected_version
                ):
                    raise ReviewConflict(
                        "This request ID was already used for another review"
                    )

                result = json.loads(previous_request["result_json"])
                result["replayed"] = True
                return result

            row = db.execute(
                "SELECT * FROM cards WHERE id = ?",
                (card_id,),
            ).fetchone()

            if row is None:
                raise LookupError("Card not found")

            if row["version"] != expected_version:
                raise ReviewConflict(
                    "This card was reviewed elsewhere. Refresh the queue."
                )

            if row["due_at"] > now.timestamp():
                raise ReviewConflict("This card is not due yet")

            before = Card.from_json(row["fsrs_json"])

            if before.last_review and now < before.last_review:
                raise ReviewConflict("Server clock moved backwards")

            after, log = self.scheduler.review_card(
                before,
                RATINGS[rating],
                review_datetime=now,
            )

            state = (
                "learnt"
                if after.state == State.Review
                else "learning"
            )

            lapse = int(
                before.state == State.Review
                and rating == "again"
            )

            db.execute(
                """
                UPDATE cards
                SET
                    fsrs_json = ?,
                    due_at = ?,
                    state = ?,
                    review_count = review_count + 1,
                    lapses = lapses + ?,
                    version = version + 1
                WHERE id = ?
                """,
                (
                    after.to_json(),
                    after.due.timestamp(),
                    state,
                    lapse,
                    card_id,
                ),
            )

            updated = db.execute(
                "SELECT * FROM cards WHERE id = ?",
                (card_id,),
            ).fetchone()

            # Store a small result snapshot, not another copy of all
            # meanings and conjugations for every review.
            result = {
                "card": self._summary(updated),
                "previous_state": row["state"],
                "replayed": False,
            }

            db.execute(
                """
                INSERT INTO srs_reviews (
                    request_id, card_id, expected_version, rating,
                    reviewed_at, review_log_json, before_json,
                    after_json, scheduler_json, result_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    card_id,
                    expected_version,
                    rating,
                    now.timestamp(),
                    log.to_json(),
                    before.to_json(),
                    after.to_json(),
                    self.scheduler_json,
                    json.dumps(result),
                ),
            )

        return result
