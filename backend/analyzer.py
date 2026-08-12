import asyncio
import google.auth
import httpx
import ipaddress
import os
import json
import re
import socket
from urllib.parse import urlparse
from dotenv import load_dotenv
from google import genai
from google.genai import types
from bs4 import BeautifulSoup

from database import SessionLocal, ScanRecord, ParentSettings

load_dotenv()

PROJECT_ID = os.getenv("GCP_PROJECT_ID")

# Which Gemini model to use for image, audio, video and website scans.
#
# Two things pushed this to flash-lite. It was originally hardcoded to
# gemini-2.5-flash in five places, which Google no longer serves to new API keys,
# so every scan 404'd. Then gemini-flash-latest ran out of free-tier quota within
# a day of normal use - screenshotting every page a child opens is a lot of vision
# calls - and every scan started failing again. flash-lite handles these
# screenshots in about 2s and has far more headroom on the free tier.
#
# Set GEMINI_MODEL in .env to something stronger if you have billing enabled and
# want more careful judgement on images.
MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")

# Text scans - above all the search queries the extension judges before a results
# page renders - sit directly in front of a page load, so latency is felt. Measured
# on this project's own queries: flash-lite averaged 0.9s against 4-7s for full
# flash, and returned the same verdict on every case tested. Vision keeps the
# stronger model, where the judgement is genuinely harder.
FAST_MODEL = os.getenv("GEMINI_FAST_MODEL", "gemini-flash-lite-latest")


class MissingCredentialsError(RuntimeError):
    """Raised when neither a Gemini API key nor a GCP project is configured."""


class ModelError(RuntimeError):
    """Raised when the Gemini call itself fails, or returns something unusable."""


_client = None


def get_client():
    """Return the Vertex AI client, creating it on first use.

    This used to be built at module import time, which meant importing anything
    from this file required working GCP credentials — so no unit test and no CI
    run could touch even the pure helpers like route_text_message(). Deferring
    construction keeps the module importable everywhere; the client is still
    created exactly once, on the first real analyze call.
    """
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        project_id = os.getenv("GCP_PROJECT_ID")

        if api_key:
            # Gemini Developer API: one key from aistudio.google.com, with no
            # gcloud install, no GCP project, and no administrator rights needed.
            # Checked first because it is by far the easier path to get running.
            _client = genai.Client(api_key=api_key)
        elif project_id:
            # Vertex AI: needs `gcloud auth application-default login` to have been
            # run, and the Vertex AI API enabled on the project.
            _client = genai.Client(
                vertexai=True,
                project=project_id,
                location="us-central1"
            )
        else:
            raise MissingCredentialsError(
                "No Gemini credentials configured. Create backend/.env with:\n"
                "    GEMINI_API_KEY=your-key-here\n"
                "and get a free key at https://aistudio.google.com/apikey\n"
                "Alternatively, to use Vertex AI instead, set GCP_PROJECT_ID and "
                "run `gcloud auth application-default login`."
            )
    return _client


async def _generate_json(model: str, contents: list) -> dict:
    """Run one Gemini call and parse its JSON reply.

    Every analyze function used to inline this, and both of the ways it reliably
    fails in practice surfaced as a bare HTTP 500 with no message: the API
    refusing the call (quota exhausted, bad key, retired model), and the model
    replying with something that is not quite JSON. From the extension both look
    identical to the feature simply being broken, which is exactly how an
    exhausted free-tier quota went unnoticed. Translate them into something a
    person can act on.
    """
    try:
        response = await asyncio.to_thread(
            get_client().models.generate_content,
            model=model,
            contents=contents,
        )
    except MissingCredentialsError:
        raise
    except Exception as exc:
        text = str(exc)
        if "RESOURCE_EXHAUSTED" in text or "429" in text:
            raise ModelError(
                f"Gemini quota exhausted for '{model}'. Free-tier keys have daily "
                "limits, and screenshotting every page uses them quickly. Wait for "
                "the reset, point GEMINI_MODEL / GEMINI_FAST_MODEL at a model you "
                "still have quota on, or enable billing on the key."
            ) from exc
        if "PERMISSION_DENIED" in text or "API key not valid" in text or "401" in text or "403" in text:
            raise ModelError(
                "Gemini rejected the API key. Check GEMINI_API_KEY in backend/.env."
            ) from exc
        if "NOT_FOUND" in text or "404" in text:
            raise ModelError(
                f"Model '{model}' is not available to this key. Set GEMINI_MODEL in "
                "backend/.env to one that is."
            ) from exc
        raise ModelError(f"Gemini call failed: {text[:300]}") from exc

    raw = (response.text or "").strip()
    clean = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError as exc:
        raise ModelError(
            f"The model replied with something that is not JSON: {clean[:200]}"
        ) from exc


def save_scan(content_type: str, input_summary: str, result: dict, user_email: str = None, source: str = "manual"):
    """Writes one scan result into the database, tagged with whoever was signed in."""
    # `with` guarantees the session is closed even if the commit raises. The old
    # open/commit/close sequence leaked the connection on any failure.
    with SessionLocal() as db:
        record = ScanRecord(
            user_email=user_email,
            source=source,
            content_type=content_type,
            # The model can return an explicit null for description/transcript, in
            # which case .get(key, default) yields None rather than the default and
            # slicing it would raise.
            input_summary=(input_summary or "")[:200],
            moderation_decision=result.get("moderation_decision", "unknown"),
            reason=result.get("reason", ""),
            confidence=result.get("confidence", 0.0),
            full_result=json.dumps(result)
        )
        db.add(record)
        db.commit()


async def analyze_text(text: str, user_email: str = None, source: str = "manual"):
    prompt = f"""
    You are Dhvanyartha, an AI content moderator specialized in Indian languages and culture.

    Analyze this text: "{text}"

    Return ONLY a JSON object with these fields:
    {{
        "language": "detected language name",
        "translation": "English translation of the text (if already in English, repeat it as-is)",
        "emotion": "primary emotion (joy/anger/sarcasm/fear/neutral)",
        "intent": "benign/harmful/satire/humor/informational",
        "cultural_context": "any Indian cultural reference detected or none",
        "moderation_decision": "allow/flag/block",
        "reason": "one line explanation of decision",
        "confidence": 0.0,
        "min_age": 0,
        "categories": [],
        "self_harm_signal": false
    }}

    This text may be a SEARCH QUERY a child has just typed. Judge what the person is
    trying to reach, not only the words themselves: "how to make a bomb" is harmful
    intent even though it is a mild-looking phrase.

    For "min_age": the youngest age this is genuinely unsuitable below, as one of
    0, 7, 13, 16 or 18 (0 = fine for all ages).

    Calibrate this carefully. Being too strict is a real failure, not a safe
    default: a child blocked from their own homework learns the tool is broken and
    finds a way around it.
    - Educational, historical, journalistic, medical or sporting treatment of a
      difficult subject is NOT adult content. "world war 2 battle history", "how
      do vaccines work", "boxing highlights", "causes of the partition of India"
      are 0 or 7.
    - Use 13 for genuinely mature themes, 16 for graphic or explicit material, and
      18 only for pornography, gratuitous gore, or practical instructions for
      seriously harming someone.
    - Judge what the person is trying to REACH, not whether a heavy word appears.
      "how to make a bomb at home" is 18 because of the intent behind it. "why did
      the atomic bomb end the war" is a history question and is 7.

    For "categories": a JSON array containing zero or more of these exact strings,
    only where they genuinely apply: "violence", "sexual_content", "profanity",
    "gambling", "drugs_alcohol", "disturbing_imagery", "hate_speech". Return [] if
    none apply. A category applies when the SUBJECT MATTER is that topic, even if
    the wording is clinical or factual.

    For "self_harm_signal": set this to true if the text shows ANY sign that the writer may
    be experiencing a self-harm, suicide, or personal mental health crisis — even if the
    text itself is measured, seeking help, or not explicitly graphic. Set it to false for
    everything else, including fictional, historical, or unrelated uses of similar words.

    Key rules:
    - Understand code-mixed Indian languages (Hinglish, Tanglish etc)
    - Don't flag humor, sarcasm or cultural expressions as harmful
    - Consider Indian cultural context before flagging
    - Return ONLY JSON, no extra text
    """

    result = await _generate_json(FAST_MODEL, [prompt])

    save_scan("text", text, result, user_email, source)

    return result


async def analyze_image(image_bytes: bytes, mime_type: str, user_email: str = None, source: str = "manual"):
    prompt = """
    You are Dhvanyartha, an AI content moderator specialized in Indian languages and culture.

    Analyze this image for content moderation purposes.

    Return ONLY a JSON object with these fields:
    {
        "description": "a detailed 2-3 sentence description covering what app/website/content this appears to be, the specific visual elements, people, objects, or text present, and the overall context of the scene",
        "detected_text": "any text visible in the image, or none",
        "content_type": "photo/meme/screenshot/artwork/document/other",
        "cultural_context": "any Indian cultural reference detected or none",
        "moderation_decision": "allow/flag/block",
        "reason": "one line explanation of decision",
        "confidence": 0.0,
        "min_age": 0,
        "categories": [],
        "self_harm_signal": false
    }

    For "min_age": give the recommended minimum viewer age as one of 0, 7, 13, 16, or 18,
    based on how mature the content is (0 = suitable for all ages).

    For "categories": return a JSON array containing zero or more of these exact strings,
    only including ones that genuinely apply: "violence", "sexual_content", "profanity",
    "gambling", "drugs_alcohol", "disturbing_imagery", "hate_speech". Return an empty
    array [] if none apply.

    Important: a category applies if the page's SUBJECT MATTER is about that topic, not
    only when it's graphically depicted. A search result, article, or definition that is
    primarily ABOUT violence, drugs, gambling, etc. should still be tagged with that
    category — being factual, educational, or encyclopedic does NOT exempt it. For
    example, an encyclopedia-style overview of "violence" that defines and categorizes
    it should be tagged "violence", even with no graphic imagery. Only leave a category
    out if the topic is genuinely unrelated or mentioned in passing without being the
    page's actual focus.

    For "self_harm_signal": set this to true if the screen shows ANY sign that the person
    using the device may be searching for, viewing, or discussing self-harm, suicide, or
    a personal mental health crisis — this includes searches like "how to kill myself",
    pages about suicide methods, or self-harm content, EVEN IF the page itself is a safe,
    protective response (like a search engine showing helpline numbers). This field is
    about flagging the underlying signal for a parent to know about, separate from whether
    the page content itself should be blocked. Set it to false for everything else,
    including the word "kill" used in unrelated contexts (games, movies, history, news).

    Don't flag humor, satire, or cultural expressions as harmful.
    Return ONLY JSON, no extra text.
    """

    result = await _generate_json(MODEL, [
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            prompt
        ])

    save_scan("image", result.get("description", "image scan"), result, user_email, source)

    return result


async def analyze_audio(audio_bytes: bytes, mime_type: str, user_email: str = None, source: str = "manual"):
    prompt = """
    You are Dhvanyartha, an AI content moderator specialized in Indian languages and culture.

    Listen to this audio and analyze it for content moderation purposes.

    Return ONLY a JSON object with these fields:
    {
        "transcript": "what was said in the audio",
        "language": "detected language name",
        "translation": "English translation of the transcript (if already in English, repeat it as-is)",
        "emotion": "primary emotion detected in tone (joy/anger/sarcasm/fear/neutral)",
        "intent": "benign/harmful/satire/humor/informational",
        "cultural_context": "any Indian cultural reference detected or none",
        "moderation_decision": "allow/flag/block",
        "reason": "one line explanation of decision",
        "confidence": 0.0
    }

    Key rules:
    - Understand code-mixed Indian languages (Hinglish, Tanglish etc)
    - Don't flag humor, sarcasm or cultural expressions as harmful
    - Consider Indian cultural context before flagging
    - Return ONLY JSON, no extra text
    """

    result = await _generate_json(MODEL, [
            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
            prompt
        ])

    save_scan("audio", result.get("transcript", "audio scan"), result, user_email, source)

    return result


async def analyze_video(video_bytes: bytes, mime_type: str, user_email: str = None, source: str = "manual"):
    prompt = """
    You are Dhvanyartha, an AI content moderator specialized in Indian languages and culture.

    Watch this video and analyze it for content moderation purposes.

    Return ONLY a JSON object with these fields:
    {
        "description": "brief description of what happens in the video",
        "transcript": "spoken content if any, or none",
        "language": "detected language name, or none",
        "content_type": "vlog/meme/news/entertainment/educational/other",
        "cultural_context": "any Indian cultural reference detected or none",
        "moderation_decision": "allow/flag/block",
        "reason": "one line explanation of decision",
        "confidence": 0.0
    }

    Key rules:
    - Understand code-mixed Indian languages (Hinglish, Tanglish etc)
    - Don't flag humor, satire or cultural expressions as harmful
    - Consider Indian cultural context before flagging
    - Return ONLY JSON, no extra text
    """

    result = await _generate_json(MODEL, [
            types.Part.from_bytes(data=video_bytes, mime_type=mime_type),
            prompt
        ])

    save_scan("video", result.get("description", "video scan"), result, user_email, source)

    return result


class UnsafeURLError(ValueError):
    """Raised when a URL points somewhere the backend must not fetch."""


def _assert_fetchable(url: str) -> None:
    """Reject URLs that point at the machine or network the backend runs on.

    /analyze-website fetches a caller-supplied URL and returns a model-written
    summary of whatever came back, and no endpoint requires authentication. Without
    this check the server acts as a read-anything proxy into places the caller
    cannot reach directly — the router admin page on the home LAN, another service
    bound to the parent's own laptop, or a cloud instance's metadata endpoint. CORS
    is open to all origins, so any page open in the family's browser can trigger
    the fetch and read the summary back.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Only http and https URLs can be scanned, not '{parsed.scheme}'")

    host = parsed.hostname
    if not host:
        raise UnsafeURLError("That URL has no host to fetch")

    try:
        resolved = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"Could not resolve host '{host}'") from exc

    for info in resolved:
        # A perfectly public-looking hostname can still resolve to a private
        # address, so judge the resolved IP rather than how the name looks.
        raw_ip = info[4][0].split("%")[0]  # strip any IPv6 zone id
        ip = ipaddress.ip_address(raw_ip)
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise UnsafeURLError(
                f"Refusing to fetch '{host}': it resolves to the non-public address {ip}"
            )


def _extract_text(html: str) -> str:
    """Synchronous HTML -> visible text. Callers keep this off the event loop."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style"]):
        tag.decompose()

    # limit length so we don't send a massive page to Gemini
    return soup.get_text(separator=" ", strip=True)[:8000]


MAX_REDIRECTS = 5


async def fetch_website_text(url: str):
    # Redirects are followed by hand so that every hop is checked. With httpx's
    # own follow_redirects=True, a public URL that redirects to 127.0.0.1 would
    # sail straight past a check done only on the original URL.
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client_http:
        for _ in range(MAX_REDIRECTS):
            await asyncio.to_thread(_assert_fetchable, url)
            response = await client_http.get(url, headers={"User-Agent": "Mozilla/5.0"})

            if not response.is_redirect:
                break

            next_request = response.next_request
            if next_request is None:
                break
            url = str(next_request.url)
        else:
            raise UnsafeURLError(f"Gave up after {MAX_REDIRECTS} redirects")

    # html.parser is pure Python and slow, and httpx's timeout is per-chunk rather
    # than a cap on total body size — so a multi-megabyte page would otherwise be
    # parsed for seconds directly on the event loop, stalling every other request,
    # including the extension's /analyze-image calls (a page the child opens during
    # that window is never moderated). Cap the input — only 8000 chars of text are
    # ever kept anyway — and run the parse on a worker thread, the same
    # asyncio.to_thread idiom already used for every Gemini call in this module.
    html = response.text[:500_000]
    return await asyncio.to_thread(_extract_text, html)


async def analyze_website(url: str, user_email: str = None, source: str = "manual"):
    page_text = await fetch_website_text(url)

    prompt = f"""
    You are Dhvanyartha, an AI content moderator specialized in Indian languages and culture.

    Analyze this website content: "{page_text}"

    Return ONLY a JSON object with these fields:
    {{
        "site_summary": "brief summary of what this website/page is about",
        "language": "detected language name",
        "content_type": "news/blog/ecommerce/social/adult/scam/other",
        "cultural_context": "any Indian cultural reference detected or none",
        "moderation_decision": "allow/flag/block",
        "reason": "one line explanation of decision",
        "confidence": 0.0
    }}

    Return ONLY JSON, no extra text.
    """

    result = await _generate_json(MODEL, [prompt])

    save_scan("website", url, result, user_email, source)

    return result


def get_scan_history(user_email: str = None, filter_decision: str = None, filter_source: str = None, limit: int = 20):
    with SessionLocal() as db:
        query = db.query(ScanRecord).order_by(ScanRecord.created_at.desc())

        if user_email:
            query = query.filter(ScanRecord.user_email == user_email)
        if filter_decision:
            query = query.filter(ScanRecord.moderation_decision == filter_decision)
        if filter_source:
            query = query.filter(ScanRecord.source == filter_source)

        records = query.limit(limit).all()

    history = []
    for r in records:
        self_harm_signal = False
        try:
            full = json.loads(r.full_result)
            self_harm_signal = bool(full.get("self_harm_signal", False))
        except Exception:
            pass

        history.append({
            "id": r.id,
            "source": r.source or "manual",
            "content_type": r.content_type,
            "input_summary": r.input_summary,
            "moderation_decision": r.moderation_decision,
            "reason": r.reason,
            "confidence": r.confidence,
            "self_harm_signal": self_harm_signal,
            # created_at is nullable in the schema, and the dashboard calls
            # new Date(...) on this value — guard so one row without a timestamp
            # can't take down the whole history request.
            "created_at": r.created_at.isoformat() if r.created_at else None
        })
    return history


def get_settings(user_email: str):
    with SessionLocal() as db:
        row = db.query(ParentSettings).filter(ParentSettings.user_email == user_email).first()

        if not row:
            return {
                "child_age": 18,
                "blocked_categories": [],
                "guard_enabled": True
            }

        # Read the values while the session is still open — once it closes the
        # instance is detached and attribute access is no longer guaranteed.
        return {
            "child_age": row.child_age,
            "blocked_categories": json.loads(row.blocked_categories or "[]"),
            "guard_enabled": row.guard_enabled
        }


def save_settings(user_email: str, child_age: int, blocked_categories: list, guard_enabled: bool):
    with SessionLocal() as db:
        row = db.query(ParentSettings).filter(ParentSettings.user_email == user_email).first()

        if row:
            row.child_age = child_age
            row.blocked_categories = json.dumps(blocked_categories)
            row.guard_enabled = guard_enabled
        else:
            row = ParentSettings(
                user_email=user_email,
                child_age=child_age,
                blocked_categories=json.dumps(blocked_categories),
                guard_enabled=guard_enabled
            )
            db.add(row)

        db.commit()


def route_text_message(text: str):
    text_lower = text.lower().strip()

    # A URL is checked FIRST, and deliberately so. The history keywords below
    # include very common words ("blocked", "flagged", "history"), and those words
    # turn up inside perfectly ordinary links —
    # https://en.wikipedia.org/wiki/History_of_India being the obvious one. When
    # the keyword check ran first, pasting such a link returned the parent's own
    # scan history and the site was never fetched or moderated at all, with
    # nothing to signal it had been skipped. Someone who pastes a link wants that
    # link checked; that intent is far less ambiguous than a stray keyword.
    url_match = re.search(r'https?://[^\s<>"\']+', text)
    if url_match:
        # Trailing sentence punctuation is not part of the URL — "look at
        # https://example.com." must not try to fetch a host ending in a period.
        url = url_match.group(0).rstrip('.,;:!?)]}\'"')
        return {"action": "website", "url": url}

    # Check if it's a history request
    history_keywords = ["history", "past scan", "what got blocked", "show me my", "blocked", "flagged"]
    if any(keyword in text_lower for keyword in history_keywords):
        if "block" in text_lower:
            return {"action": "history", "filter": "block"}
        elif "flag" in text_lower:
            return {"action": "history", "filter": "flag"}
        else:
            return {"action": "history", "filter": None}

    # Otherwise, treat it as plain text to moderate
    return {"action": "text", "content": text}