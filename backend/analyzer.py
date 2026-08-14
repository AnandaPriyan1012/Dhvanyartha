import asyncio
import base64
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


def _trust_local_certificates():
    """Trust the machine's own CA bundle if one has been exported next to this file.

    Networks that inspect TLS - most managed corporate and school networks - re-sign
    HTTPS with their own root CA. Python ships its own certificate bundle and knows
    nothing about that CA, so every call fails with:

        [SSL: CERTIFICATE_VERIFY_FAILED] self signed certificate in certificate chain

    which surfaces as a 502 on every scan. Run `python export_ca.py` to write
    corporate-ca.pem from the Windows trust store, and it is picked up from here.
    Setting the variables rather than passing a context means httpx, the Gemini SDK
    and anything else all honour it.
    """
    bundle = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corporate-ca.pem")
    if os.path.exists(bundle):
        os.environ.setdefault("SSL_CERT_FILE", bundle)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)


_trust_local_certificates()

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

# Groq is supported as a second provider with a separate free-tier quota pool.
# When one provider is used up for the day the other takes over, so scanning keeps
# working rather than silently stopping - which is what actually happens to a
# child-safety tool that quietly runs out of quota.
#
# qwen3.6-27b is the only model on Groq that accepts images, and it was verified
# to genuinely read them (it transcribed a random string it could not have
# guessed). Groq has no audio or video model here, so those stay on Gemini.
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b")

# Which provider to try first: "gemini" (default) or "groq".
PROVIDER = os.getenv("AI_PROVIDER", "gemini").strip().lower()

GEMINI_CONFIGURED = bool(
    os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GCP_PROJECT_ID")
)


# Every category the model may tag. Kept in one place so the text and image
# prompts cannot drift apart, and so the dashboard's checkboxes have a single
# source of truth.
CATEGORIES = [
    "violence",
    "sexual_content",
    "profanity",
    "gambling",
    "drugs_alcohol",
    "disturbing_imagery",
    "hate_speech",
    "self_harm",
    "eating_disorder",
    "weapons",
    "extremism",
    "predatory_contact",
    "dangerous_challenges",
    "cyberbullying",
    "scams",
]

# Shared rubric appended to both the text and the image prompt. Written once
# because a category that is judged differently for a screenshot than for the
# search that led to it produces contradictory verdicts on the same content.
RUBRIC = """
    For "categories": a JSON array containing zero or more of these exact strings,
    only where they genuinely apply. Return [] if none do.
      "violence"             fighting, killing, physical harm
      "sexual_content"       sexual or pornographic material
      "profanity"            strong or crude language
      "gambling"             betting, casinos, loot boxes, real-money wagering
      "drugs_alcohol"        recreational drugs, alcohol, vaping, buying either
      "disturbing_imagery"   gore, injury, death, body horror, deeply upsetting scenes
      "hate_speech"          abuse or dehumanisation of a group
      "self_harm"            suicide or self-injury METHODS, encouragement, or
                             communities promoting it
      "eating_disorder"      pro-anorexia or pro-bulimia material, starvation or
                             purging advice, weight-loss content aimed at minors
      "weapons"             making, buying or acquiring guns, knives or explosives
      "extremism"            terrorist, violent-extremist or radicalising material
      "predatory_contact"    adults soliciting minors, grooming, requests to move to
                             private chat, requests for photos or personal details
      "dangerous_challenges" viral stunts or challenges that injure participants
                             (choking, poisoning, burning, blackout games)
      "cyberbullying"        targeted harassment, doxxing, pile-ons, humiliation
      "scams"                phishing, fraud, fake giveaways, account or crypto theft

    Tag on SUBJECT MATTER, not only on graphic depiction. A shop selling knives is
    "weapons" even with no violence shown.

    For "educational": true when the treatment is genuinely educational, historical,
    journalistic, medical, scientific or sporting - a lesson, an encyclopedia entry,
    a news report, a documentary, a refereed sport. False for entertainment that
    simply features the topic, and false for anything instructional in how to DO
    harm. A history of WWII is educational; a video of a real killing is not; "how
    to make a bomb" is not, however factually it is phrased.

    For "self_harm_signal": true if there is ANY sign that the person may be at
    risk of self-harm or suicide, or in a mental-health crisis - including when the
    words are calm, or are asking for help.

    For "self_harm_kind", exactly one of "none", "crisis" or "promotion".

    This single field decides whether a struggling child is helped or shut out, so
    read these rules carefully and apply them literally.

      "crisis" - the PERSON is expressing their own pain, or reaching for help.
                 Anything in the first person about their own feelings or
                 intentions is ALWAYS crisis, however alarming it sounds, and so
                 is any search for support.
                 Examples, all "crisis":
                   "i want to kill myself"
                   "i want to die"
                   "i feel so alone and worthless"
                   "suicide helpline number india"
                   "how do i stop feeling like this"
                   "is it normal to think about dying"

      "promotion" - the CONTENT supplies METHODS, encouragement, glamorisation, or
                 a community pushing people toward self-harm, suicide or
                 disordered eating. It is about material that would help someone
                 do harm, not about a person in pain.
                 Examples, all "promotion":
                   "painless ways to end my life"
                   "how to hurt myself without anyone noticing"
                   "pro ana thinspo tips"
                   "ways to make someone kill themselves"

    The test: is this a person describing how they FEEL, or content supplying a
    METHOD? Feelings are crisis. Methods are promotion. When a query could be read
    either way, choose "crisis" - a child reaching out must never be shut out.
"""


# Someone describing their own pain is a child reaching out, never a page offering
# a method - and confusing the two is the worst mistake this system can make, in
# either direction. It is enforced here rather than left to the model because the
# model demonstrably gets it wrong: Groq's qwen classified "i want to kill myself"
# as self-harm PROMOTION even with that exact phrase supplied as a worked "crisis"
# example in its prompt. Acting on that would have walled a child off from the
# helpline results they were reaching for.
_FIRST_PERSON = r"(?:\bi\b|\bi'm\b|\bim\b|\bmy\b|\bme\b)"
_DISTRESS = (
    r"(?:kill(?:ing)? myself|end(?:ing)? (?:my life|it all|it)|want to die|wanna die|"
    r"suicidal|hurt myself|harm myself|cut myself|hate myself|worthless|hopeless|"
    r"want to disappear|so alone|no reason to live|can't go on|cant go on|give up on life)"
)
FIRST_PERSON_DISTRESS = re.compile(rf"{_FIRST_PERSON}.{{0,40}}?{_DISTRESS}|{_DISTRESS}.{{0,25}}?{_FIRST_PERSON}", re.I)

# Phrases that are about obtaining a method, even when written in the first person.
# These stay blocked: "i want to die" is a child in pain, "painless ways to end my
# life" is a request for instructions.
_METHOD_SEEKING = re.compile(
    r"(?:how to|ways? to|methods?|painless|quickest|fastest|easiest|best way|"
    r"without (?:anyone|being) )"
    r".{0,30}?(?:kill|die|suicide|hurt myself|harm myself|overdose|hang|"
    r"end (?:my life|it all|it))"
    r"|(?:thinspo|pro[- ]?ana|pro[- ]?mia|purge|starv)",
    re.I,
)


def classify_self_harm(text: str, model_kind: str) -> str:
    """Decide crisis vs promotion, overriding the model where it is unsafe.

    Returns "none", "crisis" or "promotion". A first-person expression of distress
    is forced to "crisis" unless it is also plainly asking for a method.
    """
    kind = (model_kind or "none").strip().lower()
    if kind not in ("none", "crisis", "promotion"):
        kind = "none"

    if not text:
        return kind

    if _METHOD_SEEKING.search(text):
        # Asking for a method is promotion regardless of how it is phrased.
        return "promotion"

    if FIRST_PERSON_DISTRESS.search(text):
        return "crisis"

    return kind


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


class QuotaExhausted(ModelError):
    """The provider refused the call because its quota is used up."""


def _parse_json_reply(raw: str) -> dict:
    """Pull a JSON object out of a model reply.

    Reasoning models (Groq's qwen among them) narrate inside <think> blocks before
    answering, and most models like to wrap JSON in code fences. Strip both, then
    fall back to the outermost braces, because a stray closing sentence after the
    JSON should not fail an otherwise good scan.
    """
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise ModelError(f"The model replied with something that is not JSON: {text[:200]}")


async def _gemini_json(model: str, prompt: str, media=None) -> dict:
    contents = [prompt] if media is None else [
        types.Part.from_bytes(data=media[0], mime_type=media[1]), prompt
    ]
    try:
        response = await asyncio.to_thread(
            get_client().models.generate_content, model=model, contents=contents
        )
    except MissingCredentialsError:
        raise
    except Exception as exc:
        text = str(exc)
        if "RESOURCE_EXHAUSTED" in text or "429" in text:
            raise QuotaExhausted(
                f"Gemini quota exhausted for '{model}'. Free-tier keys have daily "
                "limits, and screenshotting every page uses them quickly."
            ) from exc
        if "PERMISSION_DENIED" in text or "API key not valid" in text or "401" in text or "403" in text:
            raise ModelError("Gemini rejected the API key. Check GEMINI_API_KEY in backend/.env.") from exc
        if "NOT_FOUND" in text or "404" in text:
            raise ModelError(
                f"Model '{model}' is not available to this key. Set GEMINI_MODEL in backend/.env."
            ) from exc
        raise ModelError(f"Gemini call failed: {text[:300]}") from exc

    return _parse_json_reply(response.text or "")


async def _groq_json(prompt: str, media=None) -> dict:
    """Same request against Groq's OpenAI-compatible endpoint.

    Groq is a second free tier with its own quota pool, which is the whole point:
    when one provider is used up for the day, scanning keeps working instead of
    silently stopping. Called over httpx, already a dependency, so this adds none.
    """
    content = [{"type": "text", "text": prompt}]
    if media is not None:
        b64 = base64.b64encode(media[0]).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:{media[1]};base64,{b64}"}})

    async with httpx.AsyncClient(timeout=60) as http:
        res = await http.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            # qwen3.6 is a reasoning model: it spends tokens in a <think> block
            # before answering. At 1200 it reliably ran out partway through the
            # JSON, which surfaced as "the model replied with something that is
            # not JSON" on perfectly good scans. The verdict itself is ~200 tokens;
            # the rest is headroom for the thinking.
            json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": content}],
                  "max_tokens": 4000},
        )

    if res.status_code == 429:
        raise QuotaExhausted(f"Groq quota exhausted for '{GROQ_MODEL}'.")
    if res.status_code in (401, 403):
        raise ModelError("Groq rejected the API key. Check GROQ_API_KEY in backend/.env.")
    if res.status_code != 200:
        raise ModelError(f"Groq call failed ({res.status_code}): {res.text[:200]}")

    return _parse_json_reply(res.json()["choices"][0]["message"]["content"])


async def _generate_json(model: str, prompt: str, media=None, allow_groq: bool = True) -> dict:
    """Run one scan, falling back to the other provider when quota runs out.

    Both of the ways this reliably fails used to surface as a bare HTTP 500 with no
    message - the provider refusing the call, and the model returning something
    that is not JSON - and from the extension both look exactly like the feature
    being broken. That is how an exhausted free tier went unnoticed for a whole
    session. Now each is named, and a quota failure on one provider simply moves
    the request to the other.

    allow_groq is False for audio and video: Groq's vision model takes images only,
    so those must stay on Gemini.
    """
    providers = []
    if PROVIDER == "groq" and GROQ_API_KEY and allow_groq:
        providers = [("Groq", lambda: _groq_json(prompt, media))]
        if GEMINI_CONFIGURED:
            providers.append(("Gemini", lambda: _gemini_json(model, prompt, media)))
    else:
        providers = [("Gemini", lambda: _gemini_json(model, prompt, media))]
        if GROQ_API_KEY and allow_groq:
            providers.append(("Groq", lambda: _groq_json(prompt, media)))

    last = None
    for name, call in providers:
        try:
            return await call()
        except QuotaExhausted as exc:
            print(f"[dhvanyartha] {name} quota exhausted, trying the next provider")
            last = exc
        except MissingCredentialsError as exc:
            last = exc

    if last is not None:
        raise ModelError(
            f"{last} Every configured provider is out of quota. Wait for the daily "
            "reset, add a GROQ_API_KEY or GEMINI_API_KEY for a second free tier, or "
            "enable billing."
        )
    raise MissingCredentialsError(
        "No AI provider configured. Set GEMINI_API_KEY or GROQ_API_KEY in backend/.env."
    )


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
        "educational": false,
        "self_harm_signal": false,
        "self_harm_kind": "none"
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

    {RUBRIC}

    Key rules:
    - Understand code-mixed Indian languages (Hinglish, Tanglish etc)
    - Don't flag humor, sarcasm or cultural expressions as harmful
    - Consider Indian cultural context before flagging
    - Return ONLY JSON, no extra text
    """

    result = await _generate_json(FAST_MODEL, prompt)

    # Correct the model where the crisis/promotion call is unsafe to trust.
    result["self_harm_kind"] = classify_self_harm(text, result.get("self_harm_kind"))
    if result["self_harm_kind"] != "none":
        result["self_harm_signal"] = True

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
        "educational": false,
        "self_harm_signal": false,
        "self_harm_kind": "none"
    }

    For "min_age": give the recommended minimum viewer age as one of 0, 7, 13, 16, or 18,
    based on how mature the content is (0 = suitable for all ages).

    """ + RUBRIC + """

    Don't flag humor, satire, or cultural expressions as harmful.
    Return ONLY JSON, no extra text.
    """

    result = await _generate_json(MODEL, prompt, media=(image_bytes, mime_type))

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

    result = await _generate_json(MODEL, prompt, media=(audio_bytes, mime_type), allow_groq=False)

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

    result = await _generate_json(MODEL, prompt, media=(video_bytes, mime_type), allow_groq=False)

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

    result = await _generate_json(MODEL, prompt)

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