from typing import Annotated
from fastapi import APIRouter, Depends, Form, HTTPException
from pydantic import BaseModel, Field
from typing import Literal
from uuid import UUID

from core.dependencies import Services, get_services
from core.segmentation import normalize_key
from core.lexical import LexicalError, source_for
from core.srs_scheduler import ReviewConflict



router = APIRouter(prefix="/api")
ServiceDep = Annotated[Services, Depends(get_services)]


class AddCardRequest(BaseModel):
    article_id: str
    segment_id: int = Field(ge=0)
    segmentation_version: str

class ReviewRequest(BaseModel):
    rating: Literal["again", "hard", "good", "easy"]
    expected_version: int = Field(ge=0)
    request_id: UUID

@router.post("/ingest")
def api_ingest(
    services: ServiceDep,
    title: Annotated[str, Form(min_length=1, max_length=500)],
    text: Annotated[str, Form(min_length=1, max_length=200_000)],
):
    if not title.strip() or not text.strip():
        raise HTTPException(422, "Title and article text cannot be blank")

    with services.lock:
        success, result = services.corpus.add_document(title.strip(), text)

    return {"success": success, "result": result}


@router.get("/articles")
def get_articles(services: ServiceDep):
    with services.lock:
        return services.corpus.get_articles_metadata()


def resolve_occurrence(payload: AddCardRequest, services: Services):
    """Resolve server-owned source data while holding the NLP lock."""
    with services.lock:
        article = services.corpus.get_article(payload.article_id)
        if article is None:
            raise HTTPException(404, "Article not found")

        if payload.segmentation_version != services.segmenter.version:
            raise HTTPException(
                409,
                "Segmentation changed; reload the article",
            )

        segments = services.segmenter.segment(article["text"])

        if payload.segment_id >= len(segments):
            raise HTTPException(422, "Invalid segment")

        segment = dict(segments[payload.segment_id])

        if segment["kind"] not in {"word", "phrase"}:
            raise HTTPException(422, "This segment is not vocabulary")

        if len(segment["text"]) > 512:
            raise HTTPException(422, "Selected vocabulary is too long")

        return segment, source_for(article, segment)


def acquire_lookup(source, services):
    try:
        return services.lexical.lookup(source)
    except LexicalError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.post("/articles/{doc_id}/read")
def mark_article_read(doc_id: str, services: ServiceDep):
    with services.lock:
        if not services.corpus.mark_as_read(doc_id):
            raise HTTPException(404, "Article not found")

    return {"success": True}

@router.get("/articles/{doc_id}")
def get_article_content(doc_id: str, services: ServiceDep):
    with services.lock:
        article = services.corpus.get_article(doc_id)
        if article is None:
            raise HTTPException(404, "Article not found")

        saved = services.srs.states_by_key()
        segments = []

        for original in services.segmenter.segment(article["text"]):
            # Never mutate the segmenter's cached dictionaries.
            segment = dict(original)

            if segment["kind"] in {"word", "phrase"}:
                source = source_for(article, segment)
                key = services.lexical.key(source)
                segment["lookup_key"] = key
                segment["srs"] = saved.get(key)
                segment["in_srs"] = key in saved
            segments.append(segment)

        return {
            **article,
            "segments": segments,
            "segmentation_version": services.segmenter.version,
        }


@router.post("/lexical/lookup")
def lookup_vocabulary(payload: AddCardRequest, services: ServiceDep):
    _, source = resolve_occurrence(payload, services)

    # No services.lock is held during the paid network request.
    return acquire_lookup(source, services)


@router.post("/srs/cards")
def add_srs_card(payload: AddCardRequest, services: ServiceDep):
    segment, source = resolve_occurrence(payload, services)

    # Normally a cache hit because the popup already acquired the data.
    lookup = acquire_lookup(source, services)

    # Revalidate after the network operation.
    current_segment, current_source = resolve_occurrence(payload, services)

    if services.lexical.key(current_source) != lookup["lookup_key"]:
        raise HTTPException(409, "Article changed; reload it")

    created, card = services.srs.add(
        current_segment,
        payload.article_id,
        current_source["context"],
        lookup,
    )

    return {"created": created, "card": card}


@router.get("/flashcards")
def get_flashcards(
    services: ServiceDep,
    limit: int = 50,
):
    if not 1 <= limit <= 100:
        raise HTTPException(422, "limit must be between 1 and 100")

    return services.srs.review_queue(limit=limit)

@router.post("/srs/cards/{card_id}/review")
def review_card(
    card_id: str,
    payload: ReviewRequest,
    services: ServiceDep,
):
    try:
        return services.srs.review(
            card_id=card_id,
            rating=payload.rating,
            expected_version=payload.expected_version,
            request_id=str(payload.request_id),
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ReviewConflict as exc:
        raise HTTPException(409, str(exc)) from exc

