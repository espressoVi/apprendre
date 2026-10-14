import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from threading import RLock

import spacy
from fastapi import HTTPException, Request

from core.auth import BASE_DIR, DATA_DIR
from core.corpus_manager import CorpusManager
from core.lexical import LexicalService
from core.segmentation import FrenchSegmenter
from core.shared_cache import SharedLexicalCache
from core.srs_store import SRSStore


@dataclass
class Services:
    corpus: CorpusManager
    segmenter: FrenchSegmenter
    srs: SRSStore
    lock: RLock
    lexical: LexicalService


@dataclass
class SharedServices:
    nlp: object
    segmenter: FrenchSegmenter
    lexical: LexicalService
    lock: RLock
    users: dict[str, Services] = field(default_factory=dict)


def enable_wal(store):
    with store.connection() as db:
        db.execute("PRAGMA journal_mode=WAL")


@lru_cache(maxsize=1)
def get_shared() -> SharedServices:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    lock = RLock()

    try:
        nlp = spacy.load(
            "fr_core_news_sm",
            disable=["ner", "parser"],
        )
    except OSError as exc:
        raise RuntimeError(
            "French spaCy model missing from the environment"
        ) from exc

    if "sentencizer" not in nlp.pipe_names:
        nlp.add_pipe("sentencizer")

    lexicon_path = Path(
        os.environ.get(
            "FRENCH_PHRASES_PATH",
            str(BASE_DIR / "data" / "french_phrases.json"),
        )
    )

    segmenter = FrenchSegmenter(
        nlp=nlp,
        lexicon_path=lexicon_path,
        lock=lock,
    )

    cache = SharedLexicalCache(
        DATA_DIR / "shared" / "lexical.sqlite3"
    )

    return SharedServices(
        nlp=nlp,
        segmenter=segmenter,
        lexical=LexicalService(cache),
        lock=lock,
    )


def get_services(request: Request) -> Services:
    user = getattr(request.state, "user", None)

    if not user:
        raise HTTPException(401, "Authentication required")

    shared = get_shared()
    user_id = user["id"]

    # User IDs come exclusively from the authentication database.
    # They are never accepted from a request body or URL.
    with shared.lock:
        if user_id not in shared.users:
            directory = DATA_DIR / "users" / user_id
            directory.mkdir(parents=True, exist_ok=True)

            corpus = CorpusManager(
                filepath=directory / "corpus.sqlite3",
                legacy_path=None,
                nlp=shared.nlp,
            )
            srs = SRSStore(directory / "srs.sqlite3")

            enable_wal(corpus)
            enable_wal(srs)

            shared.users[user_id] = Services(
                corpus=corpus,
                segmenter=shared.segmenter,
                srs=srs,
                lock=shared.lock,
                lexical=shared.lexical,
            )

        return shared.users[user_id]
