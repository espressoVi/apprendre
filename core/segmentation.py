import hashlib
import json
import unicodedata
from collections import OrderedDict
from pathlib import Path
from threading import RLock

from spacy.language import Language
from spacy.matcher import Matcher, PhraseMatcher


def normalize_key(value: str) -> str:
    value = unicodedata.normalize("NFC", value).replace("’", "'")
    return " ".join(value.casefold().split())


class FrenchSegmenter:
    VERSION = "1"

    def __init__(
        self,
        nlp: Language,
        lexicon_path: Path,
        lock: RLock,
        cache_size: int = 32,
    ):
        self.nlp = nlp
        self.lock = lock
        self.cache_size = cache_size
        self.cache = OrderedDict()

        raw = lexicon_path.read_bytes()
        self.lexicon_hash = hashlib.sha256(raw).hexdigest()[:12]
        entries = json.loads(raw)

        self.phrases = PhraseMatcher(nlp.vocab, attr="LOWER")
        self.rules = Matcher(nlp.vocab, validate=True)
        self.metadata = {}

        for index, entry in enumerate(entries):
            name = f"expression_{index}"
            match_id = nlp.vocab.strings.add(name)

            self.metadata[match_id] = {
                "canonical": normalize_key(entry["key"]),
                "priority": entry.get("priority", 50),
            }

            forms = entry.get("forms", [])
            if forms:
                self.phrases.add(
                    name,
                    [
                        nlp.make_doc(form.replace("’", "'"))
                        for form in forms
                    ],
                )

            patterns = entry.get("patterns", [])
            if patterns:
                self.rules.add(name, patterns)

    @property
    def version(self) -> str:
        return f"{self.VERSION}:{self.lexicon_hash}"

    def segment(self, text: str) -> list[dict]:
        cache_key = hashlib.sha256(text.encode("utf-8")).hexdigest()

        # Shared lock also protects this NLP instance when corpus operations run.
        with self.lock:
            if cache_key in self.cache:
                self.cache.move_to_end(cache_key)
                return self.cache[cache_key]

            result = self._segment(text)
            self.cache[cache_key] = result

            while len(self.cache) > self.cache_size:
                self.cache.popitem(last=False)

            return result

    def _segment(self, text: str) -> list[dict]:
        # Straight and curly apostrophes get identical matching behavior.
        # Replacement preserves character count, so original offsets remain valid.
        doc = self.nlp(text.replace("’", "'"))

        candidates = []

        for match_id, start, end in [
            *self.phrases(doc),
            *self.rules(doc),
        ]:
            span = doc[start:end]
            original = text[span.start_char:span.end_char]

            # Don't turn expressions crossing line/paragraph boundaries
            # into one clickable box.
            if "\n" in original or "\r" in original:
                continue
            if any(token.is_space for token in span):
                continue

            metadata = self.metadata[match_id]
            candidates.append(
                (
                    start,
                    end,
                    metadata["canonical"],
                    metadata["priority"],
                )
            )

        # Longest-first, then explicit priority, then earliest position.
        candidates.sort(
            key=lambda item: (
                -(item[1] - item[0]),
                -item[3],
                item[0],
                item[2],
            )
        )

        occupied = set()
        selected = {}

        for start, end, canonical, _ in candidates:
            positions = range(start, end)
            if any(position in occupied for position in positions):
                continue

            occupied.update(positions)
            selected[start] = (end, canonical)

        segments = []

        def append(start, end, kind="text", canonical=None, pos=None):
            if start >= end:
                return

            segments.append({
                "id": len(segments),
                "start": start,
                "end": end,
                "text": text[start:end],
                "kind": kind,
                "canonical": canonical,
                "pos": pos,
            })

        cursor = 0
        index = 0

        while index < len(doc):
            token = doc[index]
            append(cursor, token.idx)

            if index in selected:
                end_index, canonical = selected[index]
                end_char = doc[end_index - 1].idx + len(doc[end_index - 1])

                append(
                    token.idx,
                    end_char,
                    kind="phrase",
                    canonical=canonical,
                    pos="EXPRESSION",
                )
                index = end_index
            else:
                end_char = token.idx + len(token)
                clickable = (
                    not token.is_punct
                    and not token.is_space
                    and any(character.isalpha() for character in token.text)
                )

                append(
                    token.idx,
                    end_char,
                    kind="word" if clickable else "text",
                    canonical=(
                        normalize_key(token.lemma_ or token.text)
                        if clickable else None
                    ),
                    pos=token.pos_ or "UNKNOWN" if clickable else None,
                )
                index += 1

            cursor = end_char

        append(cursor, len(text))
        return segments
