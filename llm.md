I’m interpreting “user containerization” as **both multi-user isolation and Docker deployment**.

For this two-user application, I recommend a simpler implementation than rebuilding all your existing tables:

- Each user gets their own **corpus and SRS SQLite databases**.
- User accounts and sessions live in a separate authentication database.
- The **LLM response cache remains shared**.
- The French NLP model is loaded once.
- The existing scheduler and most API code remain unchanged.

This gives each user a genuinely independent article list, read status, saved vocabulary, and review history. It also avoids changing every scheduler query correctly—a significant source of authorization bugs.

Your current `Services` dependency is global, and both stores are already encapsulated behind that dependency, so this is a natural boundary for isolation. 

**I cannot edit your local repository or run this deployment from this chat. Below are the files to add/replace and the installation procedure. Back up first.**

---

# 1. Resulting layout

Add these files:

```text
core/
  auth.py
  shared_cache.py

scripts/
  manage_users.py

static/
  session.js

templates/
  login.html

compose.yaml
.dockerignore
.env.example
```

Replace:

```text
app.py
core/dependencies.py
static/review.js
```

Make a small change to:

```text
templates/index.html
```

Runtime storage becomes:

```text
data/
  auth.sqlite3
  shared/
    lexical.sqlite3
  users/
    <user-id>/
      corpus.sqlite3
      srs.sqlite3
```

The old `data/corpus.sqlite3`, `data/corpus.jsonl`, and `data/srs.sqlite3` are **not automatically assigned to anyone**. An explicit import command below assigns them to the chosen user.

---

# 2. Authentication

## Add `core/auth.py`

This uses standard-library password hashing and opaque, database-backed sessions. No additional authentication package is required.

```python
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(
    os.environ.get("APP_DATA_DIR", str(BASE_DIR / "data"))
).resolve()

COOKIE_NAME = "apprendre_session"
SESSION_SECONDS = 30 * 24 * 60 * 60
PASSWORD_ITERATIONS = 600_000

USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 1024:
        raise ValueError("Password must contain 12–1024 characters")

    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        PASSWORD_ITERATIONS,
    ).hex()

    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    if len(password) > 1024:
        return False

    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False

        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt),
            int(iterations),
        ).hex()

        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                        ON DELETE CASCADE
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS sessions_expiry
                ON sessions(expires_at)
            """)

        # Used for roughly comparable work when a username is unknown.
        self.dummy_hash = hash_password(secrets.token_urlsafe(32))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")

        try:
            with db:
                yield db
        finally:
            db.close()

    def create_user(self, username: str, password: str) -> dict:
        username = username.strip().lower()

        if not USERNAME_PATTERN.fullmatch(username):
            raise ValueError(
                "Username must be 1–64 lowercase letters, digits, "
                "dots, underscores, or hyphens"
            )

        user = {
            "id": uuid.uuid4().hex,
            "username": username,
        }
        password_hash = hash_password(password)

        with self.connection() as db:
            db.execute(
                """
                INSERT INTO users (
                    id, username, password_hash, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    user["id"],
                    username,
                    password_hash,
                    time.time(),
                ),
            )

        return user

    def user_by_name(self, username: str):
        with self.connection() as db:
            row = db.execute(
                "SELECT id, username FROM users WHERE username = ?",
                (username.strip().lower(),),
            ).fetchone()

        return dict(row) if row else None

    def list_users(self):
        with self.connection() as db:
            rows = db.execute(
                "SELECT id, username FROM users ORDER BY username"
            ).fetchall()

        return [dict(row) for row in rows]

    def login(self, username: str, password: str):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM users WHERE username = ?",
                (username.strip().lower(),),
            ).fetchone()

        valid = verify_password(
            password,
            row["password_hash"] if row else self.dummy_hash,
        )

        if not row or not valid:
            return None

        token = secrets.token_urlsafe(32)
        now = time.time()

        with self.connection() as db:
            db.execute(
                "DELETE FROM sessions WHERE expires_at <= ?",
                (now,),
            )
            db.execute(
                """
                INSERT INTO sessions (
                    token_hash, user_id, expires_at
                ) VALUES (?, ?, ?)
                """,
                (
                    token_hash(token),
                    row["id"],
                    now + SESSION_SECONDS,
                ),
            )

        return token

    def authenticate(self, token: str | None):
        if not token or len(token) > 256:
            return None

        with self.connection() as db:
            row = db.execute(
                """
                SELECT users.id, users.username
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ?
                  AND sessions.expires_at > ?
                """,
                (token_hash(token), time.time()),
            ).fetchone()

        return dict(row) if row else None

    def logout(self, token: str | None):
        if not token:
            return

        with self.connection() as db:
            db.execute(
                "DELETE FROM sessions WHERE token_hash = ?",
                (token_hash(token),),
            )

    def set_password(self, username: str, password: str):
        encoded = hash_password(password)

        with self.connection() as db:
            user = db.execute(
                "SELECT id FROM users WHERE username = ?",
                (username.strip().lower(),),
            ).fetchone()

            if not user:
                raise ValueError("User not found")

            db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (encoded, user["id"]),
            )
            db.execute(
                "DELETE FROM sessions WHERE user_id = ?",
                (user["id"],),
            )


@lru_cache(maxsize=1)
def get_auth() -> AuthStore:
    return AuthStore(DATA_DIR / "auth.sqlite3")
```

---

# 3. Shared LLM cache

## Add `core/shared_cache.py`

Only lexical responses are shared. No cards, review history, or read state enter this database.

```python
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class SharedLexicalCache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS lexical_cache (
                    lookup_key TEXT PRIMARY KEY,
                    data_json TEXT NOT NULL,
                    model TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")

        try:
            with db:
                yield db
        finally:
            db.close()

    def get_cached(self, key):
        with self.connection() as db:
            row = db.execute(
                """
                SELECT data_json FROM lexical_cache
                WHERE lookup_key = ?
                """,
                (key,),
            ).fetchone()

        return row["data_json"] if row else None

    def delete_cached(self, key):
        with self.connection() as db:
            db.execute(
                "DELETE FROM lexical_cache WHERE lookup_key = ?",
                (key,),
            )

    def put_cached(self, key, data_json, model):
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO lexical_cache (
                    lookup_key, data_json, model, created_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lookup_key) DO NOTHING
                """,
                (key, data_json, model, time.time()),
            )
            row = db.execute(
                """
                SELECT data_json FROM lexical_cache
                WHERE lookup_key = ?
                """,
                (key,),
            ).fetchone()

        return row["data_json"]
```

Your existing `LexicalService` only needs `get_cached`, `delete_cached`, and `put_cached`, so it can use this store unchanged. 

---

# 4. User-isolated dependencies

## Replace `core/dependencies.py`

**Important:** `get_services()` is now request-scoped. Do not add `@lru_cache` to it.

Only the shared model/cache container is globally cached.

```python
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
```

Because your router already uses `Depends(get_services)`, **`routers/api.py` does not need ownership parameters added everywhere**. Its stores now physically contain only the authenticated user’s records. 

---

# 5. Application, login, CSRF protection, and caching

## Replace `app.py`

Security behavior:

- `/`, `/api/*`, and documentation endpoints require authentication.
- Browser POSTs must originate from the configured public origin.
- API requests must identify the user associated with the open page.
- An old tab cannot silently begin modifying another account after a login switch.
- Login/session cookies are secure by default.
- API and HTML responses are not cached.
- Static assets revalidate.

```python
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
                    status_code=409,
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
```

This is appropriate for a small VPN-protected deployment. It does not include public registration, password recovery, or distributed brute-force protection.

## Add `templates/login.html`

```html
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Sign in · Apprendre</title>
    <style>
        body {
            font-family: system-ui, sans-serif;
            background: #1e1e2e;
            color: #cdd6f4;
            max-width: 24rem;
            margin: 12vh auto;
            padding: 1rem;
        }
        form { display: grid; gap: 1rem; }
        label { display: grid; gap: .4rem; }
        input, button {
            font: inherit;
            padding: .8rem;
            border-radius: .4rem;
        }
        .error { color: #f38ba8; }
    </style>
</head>
<body>
    <h1>Apprendre</h1>

    {% if error %}
    <p class="error" role="alert">{{ error }}</p>
    {% endif %}

    <form method="post" action="/login">
        <label>
            Username
            <input
                name="username"
                autocomplete="username"
                maxlength="64"
                required
                autofocus
            >
        </label>

        <label>
            Password
            <input
                type="password"
                name="password"
                autocomplete="current-password"
                maxlength="1024"
                required
            >
        </label>

        <button type="submit">Sign in</button>
    </form>
</body>
</html>
```

---

# 6. Browser identity and bounded JSON requests

## Change `templates/index.html`

Immediately after `<head>`, insert:

```html
<meta name="apprendre-user" content="{{ user.id }}">
<meta name="apprendre-username" content="{{ user.username }}">
<script src="/static/session.js"></script>
```

**Do not add `defer` to `session.js`.** It installs the API identity wrapper before the other scripts run.

Keep the existing reader/details/review scripts deferred.

## Add `static/session.js`

This adds the user header even to requests made inside your existing reader implementation, without needing to rewrite that file.

```js
(() => {
    "use strict";

    const userId = document.querySelector(
        'meta[name="apprendre-user"]'
    )?.content;

    const username = document.querySelector(
        'meta[name="apprendre-username"]'
    )?.content;

    if (!userId) {
        throw new Error("Missing authenticated page identity");
    }

    const nativeFetch = window.fetch.bind(window);

    window.fetch = function(input, init) {
        const request = new Request(input, init);
        const url = new URL(request.url);

        if (
            url.origin !== window.location.origin ||
            !url.pathname.startsWith("/api/")
        ) {
            return nativeFetch(request);
        }

        const headers = new Headers(request.headers);
        headers.set("X-Apprendre-User", userId);

        return nativeFetch(new Request(request, {
            headers,
            credentials: "same-origin",
            cache: "no-store",
        }));
    };

    async function requestJSON(url, options = {}, timeoutMs = 20000) {
        const controller = new AbortController();
        const upstreamSignal = options.signal;

        const abort = () => controller.abort();

        if (upstreamSignal?.aborted) {
            controller.abort();
        } else {
            upstreamSignal?.addEventListener("abort", abort, { once: true });
        }

        const timer = setTimeout(abort, timeoutMs);

        try {
            const response = await fetch(url, {
                ...options,
                signal: controller.signal,
            });

            let body;
            try {
                body = await response.json();
            } catch (error) {
                if (controller.signal.aborted) throw error;
                throw new Error("Server returned an invalid JSON response");
            }

            if (!response.ok) {
                const detail = typeof body.detail === "string"
                    ? body.detail
                    : `Request failed (${response.status})`;

                const error = new Error(detail);
                error.status = response.status;

                if (response.status === 401) {
                    window.location.assign("/login");
                }

                throw error;
            }

            return body;
        } catch (error) {
            if (controller.signal.aborted) {
                throw new Error(
                    "Request timed out or was cancelled. Please retry."
                );
            }
            throw error;
        } finally {
            clearTimeout(timer);
            upstreamSignal?.removeEventListener("abort", abort);
        }
    }

    window.AppSession = Object.freeze({
        userId,
        requestJSON,
    });

    document.addEventListener("DOMContentLoaded", () => {
        const nav = document.querySelector("nav");

        if (nav) {
            const label = document.createElement("span");
            label.textContent = username || "Signed in";
            label.style.marginLeft = "auto";

            const form = document.createElement("form");
            form.method = "post";
            form.action = "/logout";

            const button = document.createElement("button");
            button.type = "submit";
            button.textContent = "Sign out";
            button.className = "secondary";

            form.append(button);
            nav.append(label, form);
        }

        if (typeof window.loadFlashcards !== "function") {
            const front = document.getElementById("fc-front");
            const status = document.getElementById("review-status");

            if (front) {
                front.textContent =
                    "Review interface failed to initialize.";
            }
            if (status) {
                status.textContent =
                    "Reload the page and check the browser console.";
            }
        }
    });
})();
```

For bounded requests in your inline article/sidebar code, replace:

```js
ArticleReader.requestJSON(
```

with:

```js
AppSession.requestJSON(
```

in `templates/index.html`.

For ingestion, pass a longer timeout:

```js
const data = await AppSession.requestJSON(
    "/api/ingest",
    {
        method: "POST",
        body: new FormData(form),
    },
    60000
);
```

---

# 7. Replace the review script

## Replace `static/review.js`

This incorporates:

- Correct braces.
- Bounded requests.
- User-scoped persisted pending submissions.
- Restoring pending submissions after a reload.
- Separate server-confirmation and reader-rendering errors.
- Visible loading/error states.
- The existing keyboard shortcuts.
- A clear secure-context requirement for request IDs.

```js
(() => {
    "use strict";

    const ids = [
        "flashcard", "fc-front", "fc-back",
        "review-summary", "review-state", "review-status",
        "review-flip", "review-refresh", "review-retry",
        "revise-view",
    ];

    const elements = Object.fromEntries(
        ids.map(id => [id, document.getElementById(id)])
    );

    for (const [id, element] of Object.entries(elements)) {
        if (!element) throw new Error(`Missing review element: ${id}`);
    }

    if (!window.AppSession) {
        throw new Error("Session client did not initialize");
    }

    const flashcard = elements.flashcard;
    const front = elements["fc-front"];
    const back = elements["fc-back"];
    const summary = elements["review-summary"];
    const stateBadge = elements["review-state"];
    const status = elements["review-status"];
    const flipButton = elements["review-flip"];
    const refreshButton = elements["review-refresh"];
    const retryButton = elements["review-retry"];
    const gradeButtons = [
        ...document.querySelectorAll("[data-rating]"),
    ];

    const labels = {
        new: "New",
        learning: "Learning",
        learnt: "Learnt · scheduled review",
    };

    const storageKey =
        `apprendre:pending-review:${AppSession.userId}`;

    let current = null;
    let flipped = false;
    let loading = false;
    let grading = false;
    let timer = null;
    let pendingSubmission = null;

    try {
        pendingSubmission = JSON.parse(
            sessionStorage.getItem(storageKey) || "null"
        );

        if (
            pendingSubmission &&
            (
                typeof pendingSubmission.cardId !== "string" ||
                !pendingSubmission.payload ||
                typeof pendingSubmission.payload.request_id !== "string" ||
                !["again", "hard", "good", "easy"].includes(
                    pendingSubmission.payload.rating
                ) ||
                !Number.isInteger(
                    pendingSubmission.payload.expected_version
                )
            )
        ) {
            throw new Error("Invalid pending review");
        }
    } catch {
        pendingSubmission = null;
        try {
            sessionStorage.removeItem(storageKey);
        } catch {
            // Storage may be unavailable.
        }
    }

    function persistPending() {
        if (pendingSubmission) {
            // If this fails, do not send a new review: its retry identity
            // must be recoverable after a reload.
            sessionStorage.setItem(
                storageKey,
                JSON.stringify(pendingSubmission)
            );
        } else {
            try {
                sessionStorage.removeItem(storageKey);
            } catch {
                // A stale saved request remains safe to replay.
            }
        }
    }

    function active() {
        return elements["revise-view"].classList.contains("active");
    }

    function intervalLabel(seconds) {
        if (seconds < 60) return "<1 min";
        if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
        if (seconds < 86400) return `${Math.round(seconds / 3600)} h`;
        return `${Math.round(seconds / 86400)} d`;
    }

    function updateControls() {
        const busy = loading || grading;

        flipButton.disabled =
            busy || !current || Boolean(pendingSubmission);
        refreshButton.disabled = busy || Boolean(pendingSubmission);
        retryButton.hidden = !pendingSubmission || grading;
        retryButton.disabled = busy;

        for (const button of gradeButtons) {
            const rating = button.dataset.rating;
            const label = rating[0].toUpperCase() + rating.slice(1);

            button.disabled = (
                busy || !current || !flipped || Boolean(pendingSubmission)
            );

            button.textContent = current
                ? `${label} · ${intervalLabel(current.intervals[rating])}`
                : label;
        }
    }

    function showCard(card) {
        // Render before making this card gradable.
        const fragment = document.createDocumentFragment();

        if (card) {
            if (
                typeof card.id !== "string" ||
                typeof card.front !== "string" ||
                !Number.isInteger(card.version) ||
                !card.data ||
                !card.intervals ||
                !["again", "hard", "good", "easy"].every(
                    rating => Number.isFinite(card.intervals[rating])
                )
            ) {
                throw new Error("Server returned an invalid review card");
            }

            if (!window.LexicalDetails) {
                throw new Error("Vocabulary renderer is unavailable");
            }

            LexicalDetails.render(fragment, card.data);
        }

        current = card;
        flipped = false;
        flashcard.classList.remove("flipped");
        flipButton.textContent = "Reveal answer";
        back.replaceChildren(fragment);

        front.textContent = card
            ? card.front
            : "All caught up for now.";

        stateBadge.textContent = card
            ? labels[card.state] || card.state
            : "";

        if (card) {
            stateBadge.dataset.state = card.state;
        } else {
            stateBadge.removeAttribute("data-state");
        }

        updateControls();
    }

    function scheduleRefresh(queue) {
        clearTimeout(timer);
        timer = null;

        if (current || queue.next_due == null) return;

        const delay = Math.max(
            1000,
            (queue.next_due - queue.server_now) * 1000 + 250
        );

        timer = setTimeout(() => {
            if (active() && !pendingSubmission) {
                void loadFlashcards();
            }
        }, Math.min(delay, 60000));
    }

    async function loadFlashcards() {
        if (loading || grading) return;

        if (pendingSubmission) {
            front.textContent = "A previous review needs confirmation.";
            status.textContent =
                "Use “Retry saving review” to confirm it safely.";
            updateControls();
            return;
        }

        clearTimeout(timer);
        loading = true;

        try {
            status.textContent = "Loading due cards…";
            updateControls();

            const queue = await AppSession.requestJSON(
                "/api/flashcards?limit=1"
            );

            if (
                !Array.isArray(queue.cards) ||
                !queue.counts ||
                !["new", "learning", "learnt"].every(
                    key => Number.isFinite(queue.counts[key])
                ) ||
                !Number.isFinite(queue.due_total) ||
                !Number.isFinite(queue.server_now) ||
                (
                    queue.next_due !== null &&
                    !Number.isFinite(queue.next_due)
                )
            ) {
                throw new Error("Server returned an invalid review queue");
            }

            showCard(queue.cards[0] || null);

            summary.textContent =
                `${queue.counts.new} new · ` +
                `${queue.counts.learning} learning · ` +
                `${queue.counts.learnt} learnt · ` +
                `${queue.due_total} due now`;

            status.textContent = "";

            if (!current && queue.next_due != null) {
                status.textContent =
                    "Next review: " +
                    new Date(queue.next_due * 1000).toLocaleString();
            } else if (!current) {
                status.textContent =
                    "Save vocabulary with the heart while reading.";
            }

            scheduleRefresh(queue);
        } catch (error) {
            console.error("Could not load reviews", error);
            current = null;
            flipped = false;
            flashcard.classList.remove("flipped");
            back.replaceChildren();
            stateBadge.textContent = "";
            stateBadge.removeAttribute("data-state");
            front.textContent = "Could not load due cards.";
            status.textContent = error.message;
        } finally {
            loading = false;
            updateControls();

            if (current && active()) {
                flashcard.focus({ preventScroll: true });
            }
        }
    }

    function flipCard() {
        if (!current || loading || grading || pendingSubmission) return;

        flipped = !flipped;
        flashcard.classList.toggle("flipped", flipped);
        flipButton.textContent = flipped
            ? "Hide answer"
            : "Reveal answer";

        updateControls();
    }

    async function submitReview(rating) {
        if (loading || grading) return;

        if (!pendingSubmission) {
            if (!current || !flipped) return;

            if (typeof crypto.randomUUID !== "function") {
                status.textContent =
                    "Reviews require HTTPS. Open the HTTPS proxy URL.";
                return;
            }

            pendingSubmission = {
                cardId: current.id,
                payload: {
                    rating,
                    expected_version: current.version,
                    request_id: crypto.randomUUID(),
                },
            };

            try {
                persistPending();
            } catch {
                pendingSubmission = null;
                status.textContent =
                    "Browser session storage is unavailable. " +
                    "Enable it before reviewing.";
                updateControls();
                return;
            }
        }

        grading = true;
        status.textContent = "Saving review…";
        updateControls();

        let confirmed = false;

        try {
            const submission = pendingSubmission;

            const result = await AppSession.requestJSON(
                `/api/srs/cards/${encodeURIComponent(submission.cardId)}/review`,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(submission.payload),
                }
            );

            if (!result.card || typeof result.card.id !== "string") {
                throw new Error("Invalid review confirmation");
            }

            pendingSubmission = null;
            persistPending();
            confirmed = true;

            // Rendering is not part of confirming the database write.
            try {
                window.ArticleReader?.updateCard?.(result.card);
            } catch (error) {
                console.error("Reader update failed after saved review", error);
            }
        } catch (error) {
            if (error.status === 404 || error.status === 422) {
                pendingSubmission = null;
                persistPending();
                current = null;
                front.textContent = "The pending review cannot be applied.";
                status.textContent =
                    `${error.message} Use “Refresh due cards”.`;
            } else if (error.status === 409) {
                // Preserve the request on a possible account-switch
                // conflict. Reloading restores the correct page identity.
                status.textContent =
                    `${error.message} Reload the page, then retry if offered.`;
            } else {
                status.textContent =
                    `Could not confirm the review: ${error.message} ` +
                    "Use “Retry saving review”.";
            }
        } finally {
            grading = false;
            updateControls();
        }

        if (confirmed) await loadFlashcards();
    }

    flipButton.addEventListener("click", flipCard);
    refreshButton.addEventListener("click", () => void loadFlashcards());
    retryButton.addEventListener("click", () => void submitReview());

    for (const button of gradeButtons) {
        button.addEventListener("click", () => {
            void submitReview(button.dataset.rating);
        });
    }

    flashcard.addEventListener("click", event => {
        if (
            event.target instanceof Element &&
            event.target.closest("details, summary, button, a, input")
        ) return;

        flipCard();
    });

    document.addEventListener("keydown", event => {
        if (
            !active() ||
            event.defaultPrevented ||
            event.repeat ||
            event.isComposing ||
            event.ctrlKey ||
            event.metaKey ||
            event.altKey ||
            event.shiftKey
        ) return;

        const target = event.target instanceof Element
            ? event.target
            : null;

        if (
            target &&
            (
                target.isContentEditable ||
                target.closest("input, textarea, select, [role='textbox']")
            )
        ) return;

        const rating = {
            "1": "again",
            "2": "hard",
            "3": "good",
            "4": "easy",
        }[event.key];

        if (rating) {
            if (!current || loading || grading || pendingSubmission) return;

            event.preventDefault();

            if (!flipped) {
                status.textContent =
                    "Reveal the answer before grading the card.";
                return;
            }

            void submitReview(rating);
            return;
        }

        if (event.code === "Space" || event.key === " ") {
            if (
                target?.closest(
                    "button, a[href], summary, [role='button'], [role='link']"
                )
            ) return;

            if (!current) return;

            event.preventDefault();
            flipCard();
        }

        if (event.key === "Enter" && target === flashcard) {
            event.preventDefault();
            flipCard();
        }
    });

    document.addEventListener("visibilitychange", () => {
        if (
            !document.hidden &&
            active() &&
            !current &&
            !loading &&
            !grading &&
            !pendingSubmission
        ) {
            void loadFlashcards();
        }
    });

    window.loadFlashcards = loadFlashcards;
    updateControls();
})();
```

### One necessary review-conflict adjustment

For a normal stale-card `409`, the pending review must be discardable; otherwise the retry button can remain stuck.

In `app.py`, change the account-switch response status from `409` to **`412`**:

```python
status_code=412,
```

Then, in the replacement `review.js`, use:

```js
if (
    error.status === 404 ||
    error.status === 409 ||
    error.status === 422
) {
```

for the “cannot be applied” branch, and change:

```js
} else if (error.status === 409) {
```

to:

```js
} else if (error.status === 412) {
```

This distinguishes an actual review-version conflict from an old page belonging to a different logged-in account.

---

# 8. User administration and legacy import

## Add `scripts/manage_users.py`

Run this as a module:

```bash
python -m scripts.manage_users ...
```

```python
import argparse
import getpass
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from core.auth import DATA_DIR, get_auth
from core.shared_cache import SharedLexicalCache


def backup_database(source: Path, destination: Path):
    source_uri = source.resolve().as_uri() + "?mode=ro"

    with sqlite3.connect(source_uri, uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)


def import_legacy(username: str, source: Path):
    user = get_auth().user_by_name(username)
    if not user:
        raise ValueError("Create the user first")

    source = source.resolve()
    parent = DATA_DIR / "users"
    parent.mkdir(parents=True, exist_ok=True)

    destination = parent / user["id"]
    if destination.exists():
        raise ValueError(
            "User storage already exists. Import before first login."
        )

    corpus = source / "corpus.sqlite3"
    srs = source / "srs.sqlite3"
    legacy_jsonl = source / "corpus.jsonl"

    if not corpus.exists() and legacy_jsonl.exists():
        raise ValueError(
            "Only corpus.jsonl exists. First run the old application "
            "once to migrate it to corpus.sqlite3."
        )

    if not corpus.exists() and not srs.exists():
        raise ValueError("No legacy databases found")

    stage = Path(tempfile.mkdtemp(
        prefix=".import-",
        dir=parent,
    ))

    try:
        if corpus.exists():
            backup_database(corpus, stage / "corpus.sqlite3")

        if srs.exists():
            backup_database(srs, stage / "srs.sqlite3")

            shared = SharedLexicalCache(
                DATA_DIR / "shared" / "lexical.sqlite3"
            )

            uri = srs.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as src:
                exists = src.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'lexical_cache'
                    """
                ).fetchone()

                if exists:
                    with shared.connection() as dst:
                        for row in src.execute(
                            """
                            SELECT lookup_key, data_json, model, created_at
                            FROM lexical_cache
                            """
                        ):
                            dst.execute(
                                """
                                INSERT INTO lexical_cache (
                                    lookup_key, data_json, model, created_at
                                )
                                VALUES (?, ?, ?, ?)
                                ON CONFLICT(lookup_key) DO NOTHING
                                """,
                                row,
                            )

        os.rename(stage, destination)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    print(f"Imported legacy progress for {user['username']}")


def password_prompt():
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")

    if password != confirmation:
        raise ValueError("Passwords do not match")

    return password


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    create.add_argument("username")

    password = commands.add_parser("password")
    password.add_argument("username")

    commands.add_parser("list")

    legacy = commands.add_parser("import-legacy")
    legacy.add_argument("username")
    legacy.add_argument("--source", type=Path, required=True)

    args = parser.parse_args()

    if args.command == "create":
        user = get_auth().create_user(
            args.username,
            password_prompt(),
        )
        print(f"Created {user['username']} ({user['id']})")

    elif args.command == "password":
        get_auth().set_password(
            args.username,
            password_prompt(),
        )
        print("Password changed; existing sessions revoked")

    elif args.command == "list":
        for user in get_auth().list_users():
            print(f"{user['username']}\t{user['id']}")

    elif args.command == "import-legacy":
        import_legacy(args.username, args.source)


if __name__ == "__main__":
    main()
```

Run legacy import **with the app stopped**. The command intentionally refuses to overwrite an existing user directory.

---

# 9. Container files

## Add `.dockerignore`

```dockerignore
.git
.venv
.env
.env.*
__pycache__
**/__pycache__
*.pyc
node_modules
backups

data/*
!data/french_phrases.json
```

The model’s phrase lexicon is included in the build. Databases and secrets are not.

## Add `.env.example`

```dotenv
APP_BIND_IP=127.0.0.1
APP_PORT=8000
APP_UID=1000
APP_GID=1000

APP_PUBLIC_ORIGIN=https://apprendre.example.net
APP_COOKIE_SECURE=true

DEEPSEEK_API_KEY=replace-me
DEEPSEEK_MODEL=deepseek-flash
LEXICAL_CACHE_VERSION=1
```

## Add `compose.yaml`

Requires a recent Docker Compose supporting inline Dockerfiles.

```yaml
name: apprendre

services:
  app:
    image: apprendre:local

    build:
      context: .
      dockerfile_inline: |
        FROM node:22-slim AS syntax
        WORKDIR /check
        COPY static ./static
        RUN for file in static/*.js; do node --check "$$file" || exit 1; done \
            && touch /checked

        FROM python:3.14-slim

        COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

        ENV PYTHONDONTWRITEBYTECODE=1 \
            PYTHONUNBUFFERED=1 \
            UV_PROJECT_ENVIRONMENT=/app/.venv

        WORKDIR /app
        COPY . /app

        COPY --from=syntax /checked /app/.syntax-checked
        COPY data/french_phrases.json /app/assets/french_phrases.json

        RUN uv sync --frozen --no-dev --no-install-project \
            && uv pip install --python /app/.venv/bin/python pip \
            && /app/.venv/bin/python -m spacy download fr_core_news_sm \
            && /app/.venv/bin/python -m compileall -q app.py core routers scripts

        CMD ["/app/.venv/bin/uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

    init: true
    restart: unless-stopped

    user: "${APP_UID:-1000}:${APP_GID:-1000}"

    ports:
      - "${APP_BIND_IP:-127.0.0.1}:${APP_PORT:-8000}:8000"

    environment:
      APP_DATA_DIR: /app/data
      FRENCH_PHRASES_PATH: /app/assets/french_phrases.json

      APP_PUBLIC_ORIGIN: "${APP_PUBLIC_ORIGIN:?Set APP_PUBLIC_ORIGIN}"
      APP_COOKIE_SECURE: "${APP_COOKIE_SECURE:-true}"

      DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY:?Set DEEPSEEK_API_KEY}"
      DEEPSEEK_MODEL: "${DEEPSEEK_MODEL:-deepseek-flash}"
      LEXICAL_CACHE_VERSION: "${LEXICAL_CACHE_VERSION:-1}"

      HOME: /tmp

    volumes:
      - ./data:/app/data

    read_only: true

    tmpfs:
      - /tmp:size=256m,mode=1777

    cap_drop:
      - ALL

    security_opt:
      - no-new-privileges:true

    stop_grace_period: 90s

    healthcheck:
      test:
        - CMD
        - /app/.venv/bin/python
        - -c
        - "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 180s

    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

Your file tree indicates Python 3.14. If `pyproject.toml` specifies another supported version, change the Python base image accordingly.

The application dependencies are installed from `uv.lock`. The spaCy model is selected at build time for compatibility with the locked spaCy version. For completely reproducible builds, pin the model wheel in your dependency lock and pin the base-image digests after validating the deployment.

---

# 10. Step-by-step installation

## Step 1: Stop the existing application and back up

On the existing machine:

```bash
# Stop Uvicorn first.

mkdir -p backups
tar -czf \
  "backups/pre-multiuser-$(date +%Y%m%d-%H%M%S).tar.gz" \
  data
```

Keep a copy outside the application directory.

Do not delete the original databases.

## Step 2: Apply the files above

Before building, verify:

- `index.html` contains the identity meta tags.
- `session.js` is included before the deferred scripts.
- The account-switch response uses `412`.
- Review conflicts use `409`.
- The original missing brace is gone.

## Step 3: Copy the project to the homelab

For example:

```text
/opt/apprendre
```

Use the real source repository, including:

```text
pyproject.toml
uv.lock
data/french_phrases.json
```

## Step 4: Create configuration

```bash
cd /opt/apprendre
cp .env.example .env
chmod 600 .env
```

Edit `.env`:

```dotenv
APP_PUBLIC_ORIGIN=https://apprendre.your-domain.example
APP_COOKIE_SECURE=true
DEEPSEEK_API_KEY=your-key
```

`APP_PUBLIC_ORIGIN` must exactly match the browser-facing origin:

- Correct scheme.
- Correct hostname.
- Port if nonstandard.
- No trailing path.

### Choose the bind address

If NPM runs directly on the host:

```dotenv
APP_BIND_IP=127.0.0.1
```

If NPM runs in a regular bridge-networked Docker container or on another machine:

```dotenv
APP_BIND_IP=192.168.1.50
```

Replace that address with the homelab host’s reachable LAN/VPN address.

A separate NPM container cannot reach this application through **its own** `127.0.0.1`.

Restrict access to the published port with your network/firewall configuration; do not expose it directly to the Internet.

## Step 5: Set directory permissions

```bash
mkdir -p data
sudo chown -R 1000:1000 data
sudo chmod -R u+rwX data
```

If using another UID/GID, configure those values in `.env` and use matching ownership.

## Step 6: Validate and build

```bash
docker compose config --quiet
docker compose build --pull
```

The build checks JavaScript syntax, including `review.js`, before producing the final image.

## Step 7: Create both users before starting the app

```bash
docker compose run --rm --no-deps app \
  /app/.venv/bin/python -m scripts.manage_users create alice
```

Then:

```bash
docker compose run --rm --no-deps app \
  /app/.venv/bin/python -m scripts.manage_users create bob
```

Passwords are prompted interactively and are not put in shell history.

List accounts:

```bash
docker compose run --rm --no-deps app \
  /app/.venv/bin/python -m scripts.manage_users list
```

## Step 8: Optionally import existing progress

Do this **before the chosen user first uses the application**:

```bash
docker compose run --rm --no-deps app \
  /app/.venv/bin/python -m scripts.manage_users \
  import-legacy alice --source /app/data
```

This:

- Copies the old corpus database into Alice’s directory.
- Copies the old SRS database, preserving scheduling and review history.
- Copies the old LLM cache into the shared lexical cache.
- Leaves the original databases intact.
- Leaves Bob with an empty corpus and SRS.

Because article databases are separate, Bob does not automatically receive Alice’s articles.

## Step 9: Start

```bash
docker compose up -d
docker compose logs -f app
```

Check:

```bash
docker compose ps
```

And, using the configured bind address:

```bash
curl -f http://192.168.1.50:8000/healthz
```

Expected:

```json
{"status":"ok"}
```

Opening the HTTP upstream directly is not the normal login path when secure cookies are enabled. Use the HTTPS hostname next.

## Step 10: Configure Nginx Proxy Manager

Create a Proxy Host:

| Setting | Value |
|---|---|
| Domain | Your configured application hostname |
| Forward scheme | `http` |
| Forward host | Homelab host LAN/VPN address |
| Forward port | `8000` |
| Cache assets | Off |
| SSL certificate | Valid certificate |
| Force SSL | On |

Advanced configuration:

```nginx
proxy_connect_timeout 10s;
proxy_read_timeout 120s;
proxy_send_timeout 120s;
client_max_body_size 2m;
```

Use a dedicated hostname, not `/apprendre` under another site.

The browser uses HTTPS; NPM forwards to the app over HTTP. This also makes `crypto.randomUUID()` available for review request IDs.

## Step 11: Verify isolation

Use **two separate browser profiles**:

1. Sign in as Alice.
2. Confirm imported articles and progress.
3. Sign in as Bob in another profile.
4. Confirm Bob starts empty.
5. Ingest an article as Bob.
6. Confirm Alice’s article list does not change.
7. Save and review vocabulary as Bob.
8. Confirm Alice’s SRS counts do not change.
9. Restart the container:

   ```bash
   docker compose restart app
   ```

10. Confirm both users’ state persists.

Also test:

- Incorrect password.
- Logged-out access to `/api/articles`.
- A stale open tab after switching accounts.
- Review request interrupted and retried.
- Two tabs reviewing the same card.

---

# 11. Maintenance

## Change a password

```bash
docker compose run --rm --no-deps app \
  /app/.venv/bin/python -m scripts.manage_users password alice
```

Existing sessions for that user are revoked.

## Back up

Use a stopped backup to include SQLite databases and WAL files consistently:

```bash
cd /opt/apprendre
mkdir -p backups

(
    set -eu
    docker compose stop app
    trap 'docker compose start app' EXIT

    sudo tar -czf \
      "backups/data-$(date +%Y%m%d-%H%M%S).tar.gz" \
      data
)
```

Schedule this nightly and copy backups off-host. Back up `.env` securely as well.

## Update

```bash
# Back up first.
git pull --ff-only
docker compose build --pull
docker compose up -d
docker compose logs --tail=100 app
```

**Do not run the old single-user application against the new per-user storage.** Keep the original backup until the multi-user deployment and a restore have both been verified.

### Final scope note

This implementation isolates users by database rather than by adding `user_id` to every existing table. For two users, that is straightforward and robust. If you later want a **shared article library with independent read status**, that is a different storage model; do not point both users at the same corpus database, because your existing corpus stores `read_status` directly on the article. 
