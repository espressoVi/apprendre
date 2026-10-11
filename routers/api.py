from fastapi import APIRouter, Form
from core.dependencies import corpus

router = APIRouter(prefix="/api")

@router.post("/ingest")
async def api_ingest(title: str = Form(...), text: str = Form(...)):
    success, result = corpus.add_document(title, text)
    return {"success": success, "result": result}

@router.get("/articles")
async def get_articles():
    return corpus.get_articles_metadata()

@router.get("/articles/{doc_id}")
async def get_article_content(doc_id: str):
    doc = corpus.get_article(doc_id)
    if not doc:
        return {"error": "Not found"}
    return doc

@router.post("/articles/{doc_id}/read")
async def mark_article_read(doc_id: str):
    success = corpus.mark_as_read(doc_id)
    return {"success": success}

@router.get("/flashcards/dummy")
async def get_dummy_flashcards():
    return [
        {"front": "L'ordinateur", "back": "Computer"},
        {"front": "Apprendre", "back": "To learn"},
        {"front": "Le réseau", "back": "Network"},
        {"front": "La base de données", "back": "Database"}
    ]
