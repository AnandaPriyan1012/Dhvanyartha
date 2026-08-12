from typing import Optional
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
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
    UnsafeURLError,
)
from analytics import chart_decisions, chart_content_types, chart_timeline, summary_stats

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(UnsafeURLError)
async def unsafe_url_handler(request: Request, exc: UnsafeURLError):
    """A rejected URL is the caller's mistake, so answer 400 rather than a 500."""
    return JSONResponse(status_code=400, content={"type": "error", "message": str(exc)})


class TextInput(BaseModel):
    text: str
    user_email: Optional[str] = None


class URLInput(BaseModel):
    url: str


class ChatMessage(BaseModel):
    message: str
    user_email: Optional[str] = None


class SettingsInput(BaseModel):
    user_email: str
    child_age: int
    blocked_categories: list[str]
    guard_enabled: bool = True


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
    decision: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 20,
    user_email: Optional[str] = None,
):
    return get_scan_history(user_email=user_email, filter_decision=decision, filter_source=source, limit=limit)


@app.get("/settings")
async def settings_get_endpoint(user_email: str):
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
def analytics_summary(user_email: Optional[str] = None):
    return summary_stats(user_email)


@app.get("/analytics/chart/decisions")
def analytics_chart_decisions(user_email: Optional[str] = None):
    buf = chart_decisions(user_email)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/analytics/chart/content-types")
def analytics_chart_content_types(user_email: Optional[str] = None):
    buf = chart_content_types(user_email)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/analytics/chart/timeline")
def analytics_chart_timeline(user_email: Optional[str] = None):
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