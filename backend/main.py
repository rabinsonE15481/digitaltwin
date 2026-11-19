"""DIGITALTWIN API (FastAPI). Runs locally with uvicorn and on AWS Lambda via Mangum.

GET  /api/config  -> teacher name, subject, which voice is active
POST /api/session -> short-lived OpenAI Realtime key for the browser's live WebRTC call
POST /api/ask     -> Luna routes + Sol answers (subject notes or general knowledge)
POST /api/speak   -> text -> the teacher's cloned voice (ElevenLabs) + per-character timing for lip-sync
POST /api/avatar/start|speak|interrupt|stop -> live LemonSlice avatar with her voice
GET  /api/health  -> quick check
"""
import json
import os
import time

try:  # local runs read backend/.env; on Lambda the variables come from the function config
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum
from pydantic import BaseModel, Field

import avatarlive
import brain

ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-realtime")
REALTIME_VOICE = os.getenv("REALTIME_VOICE", "marin")
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
SESSION_SECONDS = int(os.getenv("SESSION_SECONDS", "600"))

ELEVEN_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVEN_VOICE = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
ELEVEN_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5").strip()
ELEVEN_STABILITY = float(os.getenv("ELEVENLABS_STABILITY", "0.5"))
ELEVEN_SIMILARITY = float(os.getenv("ELEVENLABS_SIMILARITY", "0.85"))
CLONED_VOICE = bool(ELEVEN_KEY and ELEVEN_VOICE)

app = FastAPI(title="DIGITALTWIN")
# CORS is handled here. Do NOT also enable CORS on the Lambda Function URL (duplicate headers break browsers).
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
                   allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"])


class Turn(BaseModel):
    role: str
    content: str


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[Turn] = []


class SpeakBody(BaseModel):
    text: str = Field(min_length=1, max_length=1500)


@app.get("/api/health")
def health():
    kb = {c["n"]: bool(brain.chapter_notes(c["n"])) for c in brain.CHAPTERS}
    return {"ok": True, "chapters_loaded": kb, "router": brain.ROUTER_MODEL, "answer": brain.ANSWER_MODEL,
            "realtime": REALTIME_MODEL, "voice": "elevenlabs" if CLONED_VOICE else "openai"}


@app.get("/api/config")
def config():
    return {"teacherName": brain.TEACHER_NAME, "subject": brain.SUBJECT,
            "voice": "elevenlabs" if CLONED_VOICE else "openai",
            "avatar": "lemonslice" if avatarlive.configured() else "photo"}


# ---------- live talking avatar (LemonSlice) - needs a long-running server, not Lambda ----------
class AvatarSpeak(BaseModel):
    session_id: str
    text: str = Field(min_length=1, max_length=1500)


@app.post("/api/avatar/start")
async def avatar_start():
    if not avatarlive.configured():
        raise HTTPException(404, "Live avatar not configured (LemonSlice / LiveKit keys missing in .env)")
    try:
        return await avatarlive.start()
    except Exception as exc:
        print(json.dumps({"event": "avatar_start_error", "error": str(exc)[:500]}))
        raise HTTPException(502, f"Avatar could not start: {str(exc)[:200]}")


@app.post("/api/avatar/speak")
async def avatar_speak(body: AvatarSpeak):
    try:
        await avatarlive.speak(body.session_id, body.text)
    except KeyError as exc:
        raise HTTPException(410, str(exc))
    except Exception as exc:
        print(json.dumps({"event": "avatar_speak_error", "error": str(exc)[:500]}))
        raise HTTPException(502, f"Avatar speak failed: {str(exc)[:200]}")
    return {"ok": True}


@app.post("/api/avatar/interrupt")
async def avatar_interrupt(request: Request):
    data = json.loads((await request.body()) or b"{}")
    await avatarlive.interrupt(data.get("session_id", ""))
    return {"ok": True}


@app.post("/api/avatar/stop")
async def avatar_stop(request: Request):        # also called by navigator.sendBeacon when the page closes
    data = json.loads((await request.body()) or b"{}")
    await avatarlive.stop(data.get("session_id", ""))
    return {"ok": True}


@app.post("/api/ask")
def ask(body: AskBody):
    started = time.time()
    try:
        result = brain.ask(body.question, [t.model_dump() for t in body.history])
    except Exception as exc:
        print(json.dumps({"event": "ask_error", "q": body.question, "error": str(exc)[:500]}))
        raise HTTPException(status_code=502, detail=f"Model error: {str(exc)[:200]}")
    # One log line per question. Filter CloudWatch for "from_notes": false to see what to add to the notes.
    print(json.dumps({"event": "ask", "q": body.question, "category": result["category"],
                      "chapter": result["chapter"], "from_notes": result["from_notes"],
                      "ms": int((time.time() - started) * 1000)}))
    return result


@app.post("/api/speak")
def speak(body: SpeakBody):
    """Text -> her cloned voice (base64 MP3) + the timing of every character, which the
    browser uses to move her lips in sync with the audio."""
    if not CLONED_VOICE:
        raise HTTPException(status_code=404, detail="Cloned voice not configured (set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID)")
    r = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}/with-timestamps",
        params={"output_format": "mp3_44100_128"},
        headers={"xi-api-key": ELEVEN_KEY, "Content-Type": "application/json"},
        json={"text": body.text, "model_id": ELEVEN_MODEL,
              "voice_settings": {"stability": ELEVEN_STABILITY, "similarity_boost": ELEVEN_SIMILARITY}},
        timeout=30,
    )
    if r.status_code >= 400:
        print(json.dumps({"event": "speak_error", "status": r.status_code, "body": r.text[:300]}))
        raise HTTPException(status_code=502, detail=f"ElevenLabs error {r.status_code}: {r.text[:200]}")
    data = r.json()
    align = data.get("alignment") or data.get("normalized_alignment") or {}
    return {
        "audio_base64": data.get("audio_base64", ""),
        "chars": align.get("characters", []),
        "starts": align.get("character_start_times_seconds", []),
        "ends": align.get("character_end_times_seconds", []),
    }


@app.post("/api/session")
def session():
    session_cfg = {
        "type": "realtime",
        "model": REALTIME_MODEL,
        "instructions": brain.realtime_instructions(),
        "audio": {
            "input": {"transcription": {"model": TRANSCRIBE_MODEL}, "turn_detection": {"type": "semantic_vad"}},
            "output": {"voice": REALTIME_VOICE},
        },
        "tools": [brain.ASK_TOOL],
        "tool_choice": "auto",
    }
    if CLONED_VOICE:  # OpenAI only listens + writes text; the browser speaks it in her cloned voice
        session_cfg["output_modalities"] = ["text"]
        session_cfg["audio"].pop("output")
    r = httpx.post(
        "https://api.openai.com/v1/realtime/client_secrets",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={"expires_after": {"anchor": "created_at", "seconds": SESSION_SECONDS}, "session": session_cfg},
        timeout=20,
    )
    if r.status_code >= 400:
        print(json.dumps({"event": "session_error", "status": r.status_code, "body": r.text[:500]}))
        raise HTTPException(status_code=502, detail=f"OpenAI realtime error: {r.text[:300]}")
    data = r.json()
    return {"value": data["value"], "expires_at": data.get("expires_at"), "model": REALTIME_MODEL}


handler = Mangum(app, lifespan="off")
