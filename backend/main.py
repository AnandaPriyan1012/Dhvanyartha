import os
import re
from typing import Optional
from fastapi import FastAPI, Query, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from analyzer import CATEGORIES
from security import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from analyzer import (
    analyze_text,
    analyze_image,
    analyze_audio,
    analyze_video,
    analyze_website,
    get_scan_history,
    route_text_message,
    get_settings,
    save_settings,
    get_client,
    MissingCredentialsError,
    ModelError,
    UnsafeURLError,
)
from analytics import chart_decisions, chart_content_types, chart_timeline, summary_stats

app = FastAPI()

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(BodySizeLimitMiddleware)

# Only the dashboard's own origins may read responses from a browser page.
# allow_origins=["*"] previously meant any site the child visited could call this
# API from their browser and read back the family's settings and history.
#
# The extension is unaffected: its requests carry a chrome-extension:// origin and
# are governed by host_permissions in the manifest, not by CORS.
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:5500,http://127.0.0.1:5500"
    ).split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.exception_handler(MissingCredentialsError)
async def missing_credentials_handler(request: Request, exc: MissingCredentialsError):
    """Setup isn't finished — say exactly what to do rather than returning a 500."""
    return JSONResponse(status_code=503, content={"type": "error", "message": str(exc)})


@app.exception_handler(ModelError)
async def model_error_handler(request: Request, exc: ModelError):
    """Say what actually went wrong. A quota or key problem returning a bare 500 is
    indistinguishable from the feature being broken, which is how an exhausted
    free-tier quota went unnoticed for a whole session."""
    return JSONResponse(status_code=502, content={"type": "error", "message": str(exc)})


@app.exception_handler(UnsafeURLError)
async def unsafe_url_handler(request: Request, exc: UnsafeURLError):
    """A rejected URL is the caller's mistake, so answer 400 rather than a 500."""
    return JSONResponse(status_code=400, content={"type": "error", "message": str(exc)})


# A search query or a page's text. The cap is well above any real page extract
# (the extension sends at most 3000 chars) and stops a caller pushing an enormous
# body straight into a model prompt.
MAX_TEXT = 8000

# Deliberately not pydantic's EmailStr, which needs the email-validator package.
# This is a hobby project that has to install from requirements.txt without
# surprises, and the goal here is rejecting junk and 2MB strings, not RFC 5322
# conformance.
EMAIL_PATTERN = r"^[^@\s]{1,64}@[^@\s.]{1,63}(\.[^@\s.]{1,63})+$"
EmailQuery = Query(None, max_length=254, pattern=EMAIL_PATTERN)


def _valid_email(value: str) -> str:
    if not re.match(EMAIL_PATTERN, value or ""):
        raise ValueError("must be a valid email address")
    return value.strip().lower()


class TextInput(BaseModel):
    model_config = {"extra": "forbid"}
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    user_email: Optional[str] = Field(None, max_length=254)


class URLInput(BaseModel):
    model_config = {"extra": "forbid"}
    url: str = Field(min_length=1, max_length=2048)


class ChatMessage(BaseModel):
    model_config = {"extra": "forbid"}
    message: str = Field(min_length=1, max_length=MAX_TEXT)
    user_email: Optional[str] = Field(None, max_length=254)


class SettingsInput(BaseModel):
    """Every field is bounded. Before this, a 2MB string, an age of -999, and
    arbitrary strings as category names were all accepted and written to the
    database."""
    model_config = {"extra": "forbid"}

    user_email: str = Field(max_length=254)
    child_age: int = Field(ge=1, le=25)
    blocked_categories: list[str] = Field(default_factory=list, max_length=len(CATEGORIES))
    guard_enabled: bool = True

    @field_validator("user_email")
    @classmethod
    def check_email(cls, value: str) -> str:
        return _valid_email(value)

    @field_validator("blocked_categories")
    @classmethod
    def known_categories_only(cls, values: list[str]) -> list[str]:
        cleaned = []
        for value in values:
            slug = str(value).strip().lower()
            if slug not in CATEGORIES:
                raise ValueError(f"unknown category '{slug}'")
            if slug not in cleaned:
                cleaned.append(slug)
        return cleaned


@app.get("/health")
def health():
    """Is the server up, and is it actually able to reach Gemini?

    Worth having because every other endpoint only fails at the moment it tries to
    analyze something, which makes a setup problem look like a broken feature.
    """
    import analyzer

    providers = []
    if analyzer.GEMINI_CONFIGURED:
        providers.append(f"gemini:{analyzer.MODEL}")
    if analyzer.GROQ_API_KEY:
        providers.append(f"groq:{analyzer.GROQ_MODEL}")

    if not providers:
        return {
            "status": "ok",
            "gemini_configured": False,
            "providers": [],
            "detail": "No AI provider configured. Set GEMINI_API_KEY or GROQ_API_KEY in backend/.env.",
        }

    return {
        "status": "ok",
        "gemini_configured": analyzer.GEMINI_CONFIGURED,
        "providers": providers,
        "primary": analyzer.PROVIDER,
        "detail": f"ready to scan ({len(providers)} provider(s), falls back on quota)",
    }


@app.post("/analyze")
async def analyze(input: TextInput):
    result = await analyze_text(input.text, input.user_email, source="extension")
    return result


@app.post("/analyze-image")
async def analyze_image_endpoint(
    file: UploadFile = File(...),
    user_email: Optional[str] = Form(None),
):
    image_bytes = await file.read()
    result = await analyze_image(image_bytes, file.content_type, user_email, source="extension")
    return result


@app.post("/analyze-audio")
async def analyze_audio_endpoint(file: UploadFile = File(...)):
    audio_bytes = await file.read()
    result = await analyze_audio(audio_bytes, file.content_type)
    return result


@app.post("/analyze-video")
async def analyze_video_endpoint(file: UploadFile = File(...)):
    video_bytes = await file.read()
    result = await analyze_video(video_bytes, file.content_type)
    return result


@app.post("/analyze-website")
async def analyze_website_endpoint(input: URLInput):
    result = await analyze_website(input.url)
    return result


@app.get("/history")
async def history_endpoint(
    decision: Optional[str] = Query(None, pattern="^(allow|flag|block)$"),
    source: Optional[str] = Query(None, pattern="^(manual|extension)$"),
    # Was an unbounded int: limit=-1 and limit=999999999 both returned the whole
    # table. Now clamped, and the free-text filters are constrained to the values
    # that actually exist rather than passed through to a query.
    limit: int = Query(20, ge=1, le=200),
    user_email: Optional[str] = EmailQuery,
):
    return get_scan_history(user_email=user_email, filter_decision=decision, filter_source=source, limit=limit)


@app.get("/settings")
async def settings_get_endpoint(user_email: str = Query(..., max_length=254, pattern=EMAIL_PATTERN)):
    return get_settings(user_email)


@app.post("/settings")
async def settings_post_endpoint(input: SettingsInput):
    save_settings(input.user_email, input.child_age, input.blocked_categories, input.guard_enabled)
    return {"saved": True}


# These four are deliberately `def`, not `async def`. They do blocking work (a
# SQLite read plus a matplotlib render) with no awaits. Declared `async`, that work
# runs directly on the event loop and stalls every other request until it finishes
# — and the dashboard asks for three charts at once. As plain `def`, FastAPI runs
# them on its worker threadpool instead, keeping the server responsive.
@app.get("/analytics/summary")
def analytics_summary(user_email: Optional[str] = EmailQuery):
    return summary_stats(user_email)


@app.get("/analytics/chart/decisions")
def analytics_chart_decisions(user_email: Optional[str] = EmailQuery):
    buf = chart_decisions(user_email)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/analytics/chart/content-types")
def analytics_chart_content_types(user_email: Optional[str] = EmailQuery):
    buf = chart_content_types(user_email)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/analytics/chart/timeline")
def analytics_chart_timeline(user_email: Optional[str] = EmailQuery):
    buf = chart_timeline(user_email)
    return StreamingResponse(buf, media_type="image/png")


@app.post("/chat")
async def chat_endpoint(input: ChatMessage):
    route = route_text_message(input.message)

    if route["action"] == "history":
        results = get_scan_history(user_email=input.user_email, filter_decision=route["filter"])
        return {"type": "history", "results": results}

    elif route["action"] == "website":
        result = await analyze_website(route["url"], input.user_email)
        return {"type": "website", "result": result}

    else:
        result = await analyze_text(route["content"], input.user_email)
        return {"type": "text", "result": result}


@app.post("/chat-file")
async def chat_file_endpoint(
    file: UploadFile = File(...),
    user_email: Optional[str] = Form(None),
):
    file_bytes = await file.read()
    # content_type is None when the client sends no Content-Type header. Calling
    # .startswith() on that raises AttributeError and returns a 500, instead of
    # the friendly "unsupported file type" message this endpoint already has.
    mime = file.content_type or ""

    if mime.startswith("image/"):
        result = await analyze_image(file_bytes, mime, user_email)
        return {"type": "image", "result": result}
    elif mime.startswith("audio/"):
        result = await analyze_audio(file_bytes, mime, user_email)
        return {"type": "audio", "result": result}
    elif mime.startswith("video/"):
        result = await analyze_video(file_bytes, mime, user_email)
        return {"type": "video", "result": result}
    else:
        return {"type": "error", "message": f"Unsupported file type: {mime}"}