from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import RLock

import spacy

from core.corpus_manager import CorpusManager
from core.segmentation import FrenchSegmenter
from core.srs_store import SRSStore
from core.lexical import LexicalService


BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Services:
    corpus: CorpusManager
    segmenter: FrenchSegmenter
    srs: SRSStore
    lock: RLock
    lexical: LexicalService

@lru_cache(maxsize=1)
def get_services() -> Services:
    lock = RLock()
    srs = SRSStore(BASE_DIR / "data" / "srs.sqlite3")
    try:
        nlp = spacy.load(
            "fr_core_news_sm",
            disable=["ner", "parser"],
        )
    except OSError as exc:
        raise RuntimeError(
            "French spaCy model missing. Run: "
            "uv run python -m spacy download fr_core_news_sm"
        ) from exc

    nlp.add_pipe("sentencizer")

    return Services(
        corpus=CorpusManager(
            filepath=BASE_DIR / "data" / "corpus.sqlite3",
            legacy_path=BASE_DIR / "data" / "corpus.jsonl",
            nlp=nlp,
        ),
        segmenter=FrenchSegmenter(
            nlp=nlp,
            lexicon_path=BASE_DIR / "data" / "french_phrases.json",
            lock=lock,
        ),
        srs=srs,
        lock=lock,
        lexical=LexicalService(srs),
    )
