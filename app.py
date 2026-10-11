from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from core.dependencies import BASE_DIR, get_services
from routers import api


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_services()
    yield
    get_services.cache_clear()


app = FastAPI(title="Apprendre", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static",)
app.include_router(api.router)


@app.get("/", response_class=HTMLResponse)
async def serve_spa(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True,)
