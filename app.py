import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from core.auth import (
    BASE_DIR,
    COOKIE_NAME,
    SESSION_SECONDS,
    get_auth,
)
from core.dependencies import get_shared
from routers import api


PUBLIC_ORIGIN = os.environ.get(
    "APP_PUBLIC_ORIGIN", "http://localhost:8000"
).rstrip("/")

parsed_origin = urlsplit(PUBLIC_ORIGIN)
if (
    parsed_origin.scheme not in {"http", "https"}
    or not parsed_origin.netloc
    or parsed_origin.path
    or parsed_origin.query
    or parsed_origin.fragment
):
    raise RuntimeError(
        "APP_PUBLIC_ORIGIN must be an origin such as "
        "https://apprendre.example.net, without a path"
    )

COOKIE_SECURE = os.environ.get(
    "APP_COOKIE_SECURE", "true"
).lower() == "true"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(get_auth)
    await run_in_threadpool(get_shared)
    yield
    get_shared.cache_clear()
    get_auth.cache_clear()


app = FastAPI(
    title="Apprendre",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

templates = Jinja2Templates(
    directory=str(BASE_DIR / "templates")
)


@app.middleware("http")
async def authentication_and_headers(request: Request, call_next):
    path = request.url.path

    public = (
        path in {"/login", "/healthz"}
        or path.startswith("/static/")
    )

    # Exact Origin validation provides CSRF protection for browser writes.
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if request.headers.get("origin") != PUBLIC_ORIGIN:
            return JSONResponse(
                {"detail": "Untrusted or missing request Origin"},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )

    user = None

    if not public:
        user = await run_in_threadpool(
            get_auth().authenticate,
            request.cookies.get(COOKIE_NAME),
        )

        if not user:
            if path.startswith("/api/"):
                return JSONResponse(
                    {"detail": "Authentication required"},
                    status_code=401,
                    headers={"Cache-Control": "no-store"},
                )

            return RedirectResponse(
                "/login",
                status_code=303,
                headers={"Cache-Control": "no-store"},
            )

        request.state.user = user

        if path.startswith("/api/"):
            page_user = request.headers.get("x-apprendre-user")

            if page_user != user["id"]:
                return JSONResponse(
                    {
                        "detail": (
                            "The signed-in account changed. "
                            "Reload this page."
                        )
                    },
                    status_code=412,
                    headers={"Cache-Control": "no-store"},
                )

    response = await call_next(request)

    response.headers["Cache-Control"] = (
        "no-cache" if path.startswith("/static/") else "no-store"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"

    return response


app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)
app.include_router(api.router)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None},
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(min_length=1, max_length=64),
    password: str = Form(min_length=1, max_length=1024),
):
    token = get_auth().login(username, password)

    if not token:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Incorrect username or password."},
            status_code=401,
        )

    # Revoke any previous session presented by this browser.
    get_auth().logout(request.cookies.get(COOKIE_NAME))

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/logout")
def logout(request: Request):
    get_auth().logout(request.cookies.get(COOKIE_NAME))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/", response_class=HTMLResponse)
def serve_spa(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"user": request.state.user},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
