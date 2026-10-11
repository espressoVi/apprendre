#!/usr/bin/env python3
"""Build a conservative French expression lexicon from PARSEME 1.2.

Run from the project root. Requires spaCy and fr_core_news_sm.

The output is a recognition lexicon, not a CEFR-ranked curriculum.
"""

import argparse
import hashlib
import json
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import spacy
from spacy.matcher import Matcher, PhraseMatcher


CATEGORIES = {"VID", "LVC.full", "LVC.cause", "IRV", "MVC"}

REFLEXIVES = [
    "me", "m'", "te", "t'", "se", "s'", "nous", "vous",
]

# Allowed only as exact, observed intervening tokens.
GAP_POS = {"DET", "ADP", "ADV", "PRON", "AUX"}

# Content words may inflect. Other tokens stay surface-specific.
CONTENT_POS = {"VERB", "AUX", "NOUN", "ADJ"}


def surface(value):
    return unicodedata.normalize("NFC", value).replace("’", "'")


def normalize(value):
    return " ".join(surface(value).casefold().split())


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def unique(values):
    return [json.loads(item) for item in sorted({encoded(v) for v in values})]


def read_sentences(path):
    """Read CUPT, including files whose sentence comments delimit sentences."""
    rows = []
    reference = ""
    ordinal = 0

    with path.open(encoding="utf-8-sig") as stream:
        for line_number, raw in enumerate(stream, 1):
            line = raw.rstrip("\r\n")

            starts_sentence = (
                line.startswith("# source_sent_id =")
                or line.startswith("# sent_id =")
                or line.startswith("# text =")
            )

            if rows and (not line.strip() or starts_sentence):
                ordinal += 1
                yield rows, reference or f"sentence-{ordinal}"
                rows = []
                reference = ""

            if not line.strip():
                continue

            if line.startswith("#"):
                if (
                    line.startswith("# source_sent_id =")
                    or line.startswith("# sent_id =")
                ):
                    reference = line.split("=", 1)[1].strip()
                continue

            columns = line.split("\t")

            if len(columns) != 11:
                raise ValueError(
                    f"{path}:{line_number}: expected 11 tab-separated "
                    "columns. Download the raw .cupt file, not HTML."
                )

            # Empty nodes are not surface tokens.
            if "." in columns[0]:
                continue

            rows.append(columns)

    if rows:
        ordinal += 1
        yield rows, reference or f"sentence-{ordinal}"


def prepare_sentence(rows):
    """Reconstruct surface text and map integer CUPT IDs to char spans.

    Children of a contraction share the contraction's surface span:
    e.g. de + le both map to the characters 'du'.
    """
    words = {}
    groups = defaultdict(lambda: {"category": None, "ids": []})

    for row in rows:
        if not row[0].isdigit():
            continue

        token_id = int(row[0])
        if token_id in words:
            raise ValueError(f"Duplicate token ID: {token_id}")
        words[token_id] = row

        annotation = row[10]
        if annotation in {"*", "_"}:
            continue

        for membership in annotation.split(";"):
            identifier, separator, category = membership.partition(":")
            if not identifier.isdigit() or int(identifier) < 1:
                raise ValueError(f"Invalid MWE annotation: {membership}")

            group = groups[int(identifier)]
            group["ids"].append(token_id)

            if separator:
                previous = group["category"]
                if previous is not None and previous != category:
                    raise ValueError("Conflicting categories for one MWE")
                group["category"] = category

    pieces = []
    offsets = {}
    cursor = 0
    covered = set()

    for row in rows:
        identifier = row[0]

        if "-" in identifier:
            first, last = map(int, identifier.split("-", 1))
            ids = list(range(first, last + 1))
            if any(token_id not in words for token_id in ids):
                raise ValueError(f"Missing children for contraction {identifier}")
            covered.update(ids)
        else:
            token_id = int(identifier)
            if token_id in covered:
                continue
            ids = [token_id]

        spelling = surface(row[1])
        start, end = cursor, cursor + len(spelling)

        pieces.append(spelling)
        for token_id in ids:
            offsets[token_id] = (start, end)

        cursor = end

        # Honor explicit spacing; also protect apostrophe elision.
        if "SpaceAfter=No" not in row[9] and not spelling.endswith("'"):
            pieces.append(" ")
            cursor += 1

    for group in groups.values():
        if group["category"] is None:
            raise ValueError("MWE membership has no category declaration")

    return "".join(pieces).rstrip(), words, offsets, groups


def sentence_inputs(paths):
    for path in paths:
        for rows, reference in read_sentences(path):
            text, words, offsets, groups = prepare_sentence(rows)
            if groups:
                yield text, {
                    "file": path.name,
                    "reference": reference,
                    "words": words,
                    "offsets": offsets,
                    "groups": groups,
                }


def is_reflexive(row, category):
    form = normalize(row[1])
    lemma = normalize(row[2])

    return row[3] == "PRON" and form in REFLEXIVES and (
        category == "IRV"
        or lemma in {"se", "soi"}
        or "Reflex=Yes" in row[5]
    )


def token_rule(token, reflexive=False):
    if reflexive:
        return {"LOWER": {"IN": REFLEXIVES}}

    lemma = normalize(token.lemma_)

    if token.pos_ in CONTENT_POS and lemma and lemma != "_":
        return {"LEMMA": lemma, "POS": token.pos_}

    return {"LOWER": normalize(token.text)}


def extract(doc, metadata, group, max_gap, max_tokens):
    ids = sorted(set(group["ids"]))
    if len(ids) < 2:
        return None, "fewer_than_two_components"

    offsets = metadata["offsets"]
    start = min(offsets[token_id][0] for token_id in ids)
    end = max(offsets[token_id][1] for token_id in ids)

    span = doc.char_span(start, end, alignment_mode="strict")
    if span is None:
        return None, "token_alignment"

    if len(span) < 2:
        return None, "single_spacy_token"
    if len(span) > max_tokens:
        return None, "span_too_long"

    core = {}
    for token_id in ids:
        left, right = offsets[token_id]
        row = metadata["words"][token_id]

        matching = [
            token for token in span
            if token.idx < right and token.idx + len(token) > left
        ]
        if not matching:
            return None, "missing_component"

        for token in matching:
            core.setdefault(token.i, []).append(row)

    gaps = [token for token in span if token.i not in core]
    if len(gaps) > max_gap:
        return None, "too_many_gap_tokens"

    if any(token.is_punct or token.is_space for token in span):
        return None, "punctuation_or_whitespace"

    if any(token.pos_ not in GAP_POS for token in gaps):
        return None, "lexical_or_clausal_gap"

    pattern = []
    signature = []
    label = []

    for token in span:
        if token.i in core:
            reflexive = any(
                is_reflexive(row, group["category"])
                for row in core[token.i]
            )
            rule = token_rule(token, reflexive)
            pattern.append(rule)
            signature.append(rule)

            if reflexive:
                label.append("se")
            elif token.pos_ in {"VERB", "AUX"} and token.lemma_:
                label.append(normalize(token.lemma_))
            else:
                # Keep observed noun number and determiner agreement.
                label.append(normalize(token.text))
        else:
            # Crucially: no optional wildcard and no arbitrary POS gap.
            pattern.append({"LOWER": normalize(token.text)})

            # Determiners/prepositions help produce readable labels.
            # Incidental adverbs, auxiliaries and object clitics do not.
            if token.pos_ in {"DET", "ADP"}:
                label.append(normalize(token.text))

    key = " ".join(label).replace("' ", "'")
    if not key:
        return None, "empty_key"

    return {
        "signature": encoded(signature),
        "key": key,
        "pattern": pattern,
        "form": normalize(span.text),
        "start": span.start,
        "end": span.end,
    }, None


def load_curated(path):
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError("The curated file must contain a JSON list")

    seen = set()
    for entry in entries:
        key = normalize(entry["key"])
        if not key or key in seen:
            raise ValueError(f"Empty or duplicate curated key: {key!r}")
        if not entry.get("forms") and not entry.get("patterns"):
            raise ValueError(f"Curated entry has no matcher rules: {key}")

        entry["key"] = key
        seen.add(key)

    return entries


def curated_matchers(nlp, entries):
    rules = Matcher(nlp.vocab, validate=True)
    phrases = PhraseMatcher(nlp.vocab, attr="LOWER")
    names = {}

    for index, entry in enumerate(entries):
        name = f"curated_{index}"
        names[nlp.vocab.strings.add(name)] = entry["key"]

        if entry.get("patterns"):
            rules.add(name, entry["patterns"])

        if entry.get("forms"):
            phrases.add(
                name,
                [nlp.make_doc(surface(form)) for form in entry["forms"]],
            )

    return rules, phrases, names


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, nargs="+", required=True)
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, default=Path("data/french_phrases.json")
    )
    parser.add_argument(
        "--report", type=Path,
        default=Path("data/french_phrases.report.json"),
    )
    parser.add_argument("--model", default="fr_core_news_sm")
    parser.add_argument("--min-count", type=int, default=1)
    parser.add_argument("--max-gap", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=12)
    parser.add_argument(
        "--categories", nargs="+", choices=sorted(CATEGORIES),
        default=sorted(CATEGORIES),
    )
    args = parser.parse_args()

    if args.min_count < 1 or args.max_gap < 0 or args.max_tokens < 2:
        parser.error("Invalid count/gap/token limits")

    inputs = [path.resolve() for path in args.input]
    if len(set(inputs)) != len(inputs):
        parser.error("The same input file was supplied more than once")
    if any("blind" in path.name for path in inputs):
        parser.error("Use gold test.cupt, not test.blind.cupt")
    if args.seed.resolve() == args.output.resolve():
        parser.error("Keep the curated seed separate from generated output")

    curated = load_curated(args.seed)
    nlp = spacy.load(args.model, disable=["ner", "parser"])
    rules, phrases, names = curated_matchers(nlp, curated)

    aggregates = {}
    counters = Counter()
    rejected = []
    annotated_files = Counter()

    documents = nlp.pipe(
        sentence_inputs(inputs), as_tuples=True, batch_size=64
    )

    for doc, metadata in documents:
        counters["annotated_sentences"] += 1
        annotated_files[metadata["file"]] += 1

        # Prefer existing curated names for exact-span matches.
        curated_spans = defaultdict(list)
        for match_id, start, end in [*rules(doc), *phrases(doc)]:
            curated_spans[(start, end)].append(names[match_id])

        for group in metadata["groups"].values():
            counters["annotated_occurrences"] += 1
            category = group["category"]

            if category not in args.categories:
                counters["category_excluded"] += 1
                continue

            candidate, reason = extract(
                doc, metadata, group, args.max_gap, args.max_tokens
            )

            if reason:
                counters[f"rejected:{reason}"] += 1
                # Keep bounded references, not entire corpus sentences.
                if len(rejected) < 100:
                    rejected.append({
                        "file": metadata["file"],
                        "reference": metadata["reference"],
                        "category": category,
                        "reason": reason,
                        "component_ids": sorted(group["ids"]),
                    })
                continue

            preferred = curated_spans.get(
                (candidate["start"], candidate["end"]), []
            )
            curated_key = sorted(preferred)[0] if preferred else None

            identity = (
                f"curated:{curated_key}"
                if curated_key
                else candidate["signature"]
            )

            aggregate = aggregates.setdefault(identity, {
                "count": 0,
                "labels": Counter(),
                "patterns": [],
                "forms": set(),
                "categories": Counter(),
                "references": [],
                "curated_key": curated_key,
            })

            aggregate["count"] += 1
            aggregate["labels"][candidate["key"]] += 1
            aggregate["patterns"].append(candidate["pattern"])
            aggregate["forms"].add(candidate["form"])
            aggregate["categories"][category] += 1

            if len(aggregate["references"]) < 3:
                aggregate["references"].append({
                    "file": metadata["file"],
                    "reference": metadata["reference"],
                })

            counters["accepted_occurrences"] += 1

    missing_annotations = [
        path.name for path in inputs if not annotated_files[path.name]
    ]
    if missing_annotations:
        raise ValueError(
            "No annotated expressions found in: "
            + ", ".join(missing_annotations)
        )

    # Copy curated entries; their rules and priorities remain authoritative.
    output = {
        entry["key"]: json.loads(json.dumps(entry))
        for entry in curated
    }
    report_entries = []

    for identity, aggregate in sorted(aggregates.items()):
        if aggregate["count"] < args.min_count:
            counters["below_min_count"] += 1
            continue

        key = aggregate["curated_key"]
        if key is None:
            key = min(
                aggregate["labels"],
                key=lambda label: (
                    -aggregate["labels"][label],
                    len(label),
                    label,
                ),
            )

            entry = output.setdefault(key, {
                "key": key,
                # Below the priorities in the supplied curated seed.
                "priority": 40,
                "forms": [],
                "patterns": [],
            })

            entry["forms"] = sorted(
                set(entry.get("forms", [])) | aggregate["forms"]
            )
            entry["patterns"] = unique(
                entry.get("patterns", []) + aggregate["patterns"]
            )

        # Exact curated matches contribute evidence, not generated rules.
        report_entries.append({
            "key": key,
            "curated": aggregate["curated_key"] is not None,
            "occurrences": aggregate["count"],
            "categories": dict(sorted(aggregate["categories"].items())),
            "observed_forms": sorted(aggregate["forms"]),
            "references": aggregate["references"],
            "review_label": aggregate["curated_key"] is None,
        })

    result = sorted(output.values(), key=lambda entry: entry["key"])

    # Validate the complete file using the same matcher APIs as the app.
    curated_matchers(nlp, result)

    report = {
        "source": "PARSEME shared task 1.2, French",
        "model": args.model,
        "model_version": nlp.meta.get("version"),
        "spacy_version": spacy.__version__,
        "settings": {
            "categories": sorted(args.categories),
            "min_count": args.min_count,
            "max_gap": args.max_gap,
            "max_tokens": args.max_tokens,
        },
        "files": [
            {
                "name": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in inputs
        ],
        "seed_sha256": hashlib.sha256(
            args.seed.read_bytes()
        ).hexdigest(),
        "summary": {
            **dict(sorted(counters.items())),
            "output_entries": len(result),
        },
        "entries": sorted(
            report_entries,
            key=lambda entry: (-entry["occurrences"], entry["key"]),
        ),
        "rejected_samples": rejected,
    }

    write_json(args.report, report)
    write_json(args.output, result)

    print(f"Wrote {len(result)} entries to {args.output}")
    print(f"Report: {args.report}")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
