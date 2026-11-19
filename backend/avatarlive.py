"""Live talking avatar: LemonSlice (her face) driven by her ElevenLabs cloned voice.

How it works (LemonSlice "websocket-livekit" integration):
  1. start():  create a LiveKit room + tokens, ask LemonSlice to join it, open the audio tunnel (WebSocket).
  2. speak():  ElevenLabs streams her voice as raw PCM16 16 kHz -> we forward it into the tunnel,
               then send audio_end. LemonSlice publishes her lip-synced video + the audio into the room.
  3. The browser joins the same room (subscribe-only) and plays the video + audio.
  4. Sessions idle for AVATAR_IDLE_SECONDS are closed automatically (LemonSlice bills while a session is open).

Needs a long-running server (uvicorn locally, EC2/containers later) - NOT AWS Lambda,
because the tunnel WebSocket must stay open between requests.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import time
import uuid
from datetime import timedelta

import httpx
import websockets
from livekit import api as lkapi

SESSIONS_URL = os.getenv("LEMONSLICE_SESSIONS_URL", "https://lemonslice.com/api/liveai/sessions")
ELEVEN_BASE = os.getenv("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io")
IDLE_SECONDS = int(os.getenv("AVATAR_IDLE_SECONDS", "300"))
CHUNK_BYTES = 3200                      # 100 ms of 16 kHz mono PCM16

_sessions: dict[str, dict] = {}
_reaper: asyncio.Task | None = None


def configured() -> bool:
    need = ["LEMONSLICE_API_KEY", "LEMONSLICE_IMAGE_URL", "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
            "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"]
    return all(os.getenv(k, "").strip() for k in need)


def _token(room: str, identity: str, publish: bool) -> str:
    return (lkapi.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
            .with_identity(identity).with_name(identity)
            .with_ttl(timedelta(hours=1))
            .with_grants(lkapi.VideoGrants(room_join=True, room=room, can_publish=publish, can_subscribe=True))
            .to_jwt())


async def start() -> dict:
    global _reaper
    if _reaper is None or _reaper.done():
        _reaper = asyncio.create_task(_reap_idle())

    room = f"digitaltwin-{uuid.uuid4().hex[:12]}"
    payload = {
        "transport_type": "websocket-livekit",
        "livekit_properties": {"livekit_url": os.environ["LIVEKIT_URL"],
                               "livekit_token": _token(room, "lemonslice", publish=True)},
        "agent_image_url": os.environ["LEMONSLICE_IMAGE_URL"],
    }
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(SESSIONS_URL, json=payload,
                              headers={"X-API-Key": os.environ["LEMONSLICE_API_KEY"], "Content-Type": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"LemonSlice rejected the session: HTTP {r.status_code} {r.text[:300]}")
    data = r.json()
    address = data.get("websocket_address")
    if not address:
        raise RuntimeError("LemonSlice response has no websocket_address")

    ws = await websockets.connect(address, max_size=None)
    sid = uuid.uuid4().hex
    s = {"ws": ws, "lock": asyncio.Lock(), "last": time.time(), "room": room,
         "lemonslice_id": data.get("session_id", "")}
    s["reader"] = asyncio.create_task(_read(sid, ws))
    _sessions[sid] = s
    print(json.dumps({"event": "avatar_start", "session": sid, "room": room}))
    return {"session_id": sid, "livekit_url": os.environ["LIVEKIT_URL"],
            "token": _token(room, f"viewer-{sid[:8]}", publish=False)}


async def _read(sid: str, ws) -> None:
    """Drain events from LemonSlice (e.g. playback_finished); drop the session when the tunnel closes."""
    with contextlib.suppress(Exception):
        async for _ in ws:
            pass
    _sessions.pop(sid, None)
    print(json.dumps({"event": "avatar_closed", "session": sid}))


async def _send(ws, msg: dict) -> None:
    await ws.send(json.dumps(msg))


async def speak(sid: str, text: str) -> None:
    s = _sessions.get(sid)
    if not s:
        raise KeyError("avatar session not found (it may have timed out) - start a new one")
    s["last"] = time.time()
    voice = os.environ["ELEVENLABS_VOICE_ID"]
    body = {"text": text[:1500], "model_id": os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
            "voice_settings": {"stability": float(os.getenv("ELEVENLABS_STABILITY", "0.5")),
                               "similarity_boost": float(os.getenv("ELEVENLABS_SIMILARITY", "0.85"))}}
    async with s["lock"]:                       # one answer at a time, in order
        buf = b""
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("POST", f"{ELEVEN_BASE}/v1/text-to-speech/{voice}/stream",
                                     params={"output_format": "pcm_16000"}, json=body,
                                     headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}) as r:
                if r.status_code >= 400:
                    raise RuntimeError(f"ElevenLabs error {r.status_code}: {(await r.aread())[:200]!r}")
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    while len(buf) >= CHUNK_BYTES:  # stream to the avatar as soon as audio arrives
                        part, buf = buf[:CHUNK_BYTES], buf[CHUNK_BYTES:]
                        await _send(s["ws"], {"command": "audio", "audio": base64.b64encode(part).decode(),
                                              "sampleRate": 16000, "encoding": "PCM16"})
        if len(buf) % 2:
            buf = buf[:-1]
        if buf:
            await _send(s["ws"], {"command": "audio", "audio": base64.b64encode(buf).decode(),
                                  "sampleRate": 16000, "encoding": "PCM16"})
        await _send(s["ws"], {"command": "audio_end"})   # commit the turn - required before it renders
        s["last"] = time.time()


async def interrupt(sid: str) -> None:
    s = _sessions.get(sid)
    if s:
        with contextlib.suppress(Exception):
            await _send(s["ws"], {"command": "interrupt"})


async def stop(sid: str) -> None:
    s = _sessions.pop(sid, None)
    if not s:
        return
    with contextlib.suppress(Exception):
        await _send(s["ws"], {"command": "terminate"})
    with contextlib.suppress(Exception):
        await s["ws"].close()
    print(json.dumps({"event": "avatar_stop", "session": sid}))


async def _reap_idle() -> None:
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for sid, s in list(_sessions.items()):
            if now - s["last"] > IDLE_SECONDS:
                await stop(sid)


def touch(sid: str) -> None:
    if sid in _sessions:
        _sessions[sid]["last"] = time.time()
