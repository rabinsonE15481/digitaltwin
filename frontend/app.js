/* DIGITALTWIN - browser side.
 * Live mode: browser <-> OpenAI Realtime over WebRTC listens to the student.
 *   - Cloned voice configured on the backend (ElevenLabs): OpenAI replies with TEXT only and every reply is
 *     spoken in HER voice via /api/speak, sentence by sentence (first sentence starts playing early).
 *   - Not configured: OpenAI speaks with its preset voice.
 * Face: LemonSlice live avatar (if configured) - her real face lip-synced to her voice, over LiveKit.
 *   Otherwise avatar.js animates her photo; ElevenLabs character timings drive the lip-sync.
 * Real questions: the realtime model calls "ask_teacher" -> /api/ask (Luna routes, Sol answers from the
 *   subject notes or general knowledge) -> full answer on screen, short spoken version back to the twin.
 */
const CFG = window.TWIN_CONFIG || {};
const $ = (id) => document.getElementById(id);
let CLONE = false;
let AVATAR_LIVE = false;          // LemonSlice live face configured on the backend
let avatarSession = null;        // { id, room }
let avatarStarting = null;       // promise while connecting
let TEACHER = "your teacher";

let pc = null, dc = null, micStream = null, live = false;
let audioCtx = null, analyser = null, speakingUntil = 0, ttsSpeaking = false;
const history = [];
const handledCalls = new Set();
const speakQueue = [];
let speakingNow = false;
let answerShown = false;   // live mode: the full answer is already on screen, don't repeat the spoken version

/* ---------- helpers ---------- */
function setStatus(text, kind = "") { const s = $("status"); s.textContent = text; s.className = "status " + kind; }
function remember(role, content) { history.push({ role, content: String(content).slice(0, 2000) }); while (history.length > 8) history.shift(); }

async function api(path, body) {
  const r = await fetch(CFG.apiBase + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = await r.text();
    try { detail = JSON.parse(detail).detail || detail; } catch (e) {}
    throw new Error(`${r.status}: ${String(detail).slice(0, 200)}`);
  }
  return r;
}
const apiJson = async (path, body) => (await api(path, body)).json();

/* ---------- Markdown + LaTeX ---------- */
function escapeHtml(s) { return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function renderMarkdownMath(text) {
  const math = [];
  const protectedText = text.replace(/(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$[^$\n]+?\$)/g, (m) => {
    math.push(m); return `@@MATH${math.length - 1}@@`;
  });
  if (!window.marked || !window.DOMPurify)          // libraries didn't load: show plain text
    return escapeHtml(text).replace(/\n/g, "<br>");
  const html = DOMPurify.sanitize(marked.parse(protectedText));
  return html.replace(/@@MATH(\d+)@@/g, (_, i) => escapeHtml(math[+i]));
}
function typeset(el) {
  if (window.renderMathInElement) {
    renderMathInElement(el, {
      delimiters: [
        { left: "$$", right: "$$", display: true }, { left: "\\[", right: "\\]", display: true },
        { left: "$", right: "$", display: false }, { left: "\\(", right: "\\)", display: false },
      ],
      throwOnError: false,
    });
  }
}
function addMsg(role, text, cls = "") {
  const el = document.createElement("div");
  el.className = `msg ${role} ${cls}`.trim();
  el.textContent = text;
  $("messages").appendChild(el);
  el.scrollIntoView({ behavior: "smooth", block: "end" });
  return el;
}
function renderAnswer(el, res) {
  const badge = res.from_notes
    ? `<span class="badge notes">📘 From class notes · Ch ${res.chapter}: ${escapeHtml(res.chapter_title)}</span>`
    : `<span class="badge ai">🤖 General knowledge · not from class notes</span>`;
  el.classList.remove("pending");
  el.innerHTML = badge + renderMarkdownMath(res.display || res.speech || "");
  typeset(el);
  el.scrollIntoView({ behavior: "smooth", block: "end" });
}
async function askBrain(question) {
  const el = addMsg("twin", "Let me think…", "pending");
  try {
    const res = await apiJson("/api/ask", { question, history: history.slice(-8) });
    renderAnswer(el, res);
    remember("user", question);
    remember("assistant", res.display || res.speech);
    return res;
  } catch (err) {
    el.classList.remove("pending"); el.classList.add("error");
    el.textContent = "Sorry, I couldn't answer that right now. " + err.message;
    return null;
  }
}

/* ---------- audio level -> talking/idle video loops ---------- */
function setTalking(on) { document.body.classList.toggle("talking", !!on); }
function initAvatar() {
  ["idleVid", "talkVid"].forEach((id) => $(id).addEventListener("error", () => document.body.classList.add("novideo")));
  $("faceImg").addEventListener("error", () => { $("faceImg").style.visibility = "hidden"; });
}
function ensureAudio() {
  if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  if (!analyser) {
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 512;
    const buf = new Uint8Array(analyser.fftSize);
    const tick = () => {
      analyser.getByteTimeDomainData(buf);
      let sum = 0;
      for (const v of buf) { const d = (v - 128) / 128; sum += d * d; }
      const now = performance.now();
      const rms = Math.sqrt(sum / buf.length);
      if (rms > 0.02) speakingUntil = now + 350;
      if (window.TwinAvatar)                    // browser voice has no audio stream: gentle talking motion
        TwinAvatar.setLevel(ttsSpeaking && !CLONE ? 0.08 + 0.07 * Math.abs(Math.sin(now / 90)) : rms);
      setTalking(ttsSpeaking || now < speakingUntil);
      if (avatarSession && live) setMic(now >= speakingUntil);   // the mic must not hear her live avatar
      requestAnimationFrame(tick);
    };
    tick();
  }
  return audioCtx.resume();
}
function watchStream(stream) { audioCtx.createMediaStreamSource(stream).connect(analyser); }
function setMic(on) { if (micStream) micStream.getAudioTracks().forEach((t) => { t.enabled = on; }); }

/* ---------- live talking avatar (LemonSlice over LiveKit) ---------- */
async function ensureLiveAvatar() {
  if (!AVATAR_LIVE || !window.LivekitClient) return null;
  if (avatarSession) return avatarSession;
  if (avatarStarting) return avatarStarting;
  avatarStarting = (async () => {
    setStatus("Connecting her live avatar… (about 10-20 seconds)");
    try {
      const info = await apiJson("/api/avatar/start", {});
      const room = new LivekitClient.Room({ adaptiveStream: true });
      room.on(LivekitClient.RoomEvent.TrackSubscribed, (track) => {
        if (track.kind === "video") {
          track.attach($("liveVid"));
          document.body.classList.add("liveavatar");
        } else if (track.kind === "audio") {
          track.attach($("liveAudio"));
          ensureAudio().then(() => watchStream(new MediaStream([track.mediaStreamTrack])));
        }
      });
      room.on(LivekitClient.RoomEvent.Disconnected, () => {
        avatarSession = null;
        document.body.classList.remove("liveavatar");
      });
      await room.connect(info.livekit_url, info.token);
      avatarSession = { id: info.session_id, room };
      setStatus(live ? "Live: speak any time" : "Her avatar is ready", live ? "live" : "");
      return avatarSession;
    } catch (err) {
      console.warn("Live avatar unavailable, using the photo avatar instead", err);
      setStatus("Live avatar unavailable - using her photo. " + err.message, "error");
      AVATAR_LIVE = false;                       // fall back for the rest of this visit
      return null;
    } finally {
      avatarStarting = null;
    }
  })();
  return avatarStarting;
}
function stopLiveAvatar() {
  if (!avatarSession) return;
  const body = JSON.stringify({ session_id: avatarSession.id });
  navigator.sendBeacon ? navigator.sendBeacon(CFG.apiBase + "/api/avatar/stop", body)
                       : fetch(CFG.apiBase + "/api/avatar/stop", { method: "POST", body });
  try { avatarSession.room.disconnect(); } catch (e) {}
  avatarSession = null;
  document.body.classList.remove("liveavatar");
}
const avatarSpeakQueue = [];
let avatarSpeaking = false;
async function speakAvatar(text) {
  avatarSpeakQueue.push(text);
  if (avatarSpeaking) return;
  avatarSpeaking = true;
  while (avatarSpeakQueue.length) {
    const t = avatarSpeakQueue.shift();
    try { await apiJson("/api/avatar/speak", { session_id: avatarSession.id, text: t }); }
    catch (err) {
      if (String(err.message).startsWith("410")) { avatarSession = null; document.body.classList.remove("liveavatar"); }
      setStatus("Avatar voice error: " + err.message, "error");
    }
  }
  avatarSpeaking = false;
}

/* ---------- her cloned voice: sentence by sentence ---------- */
function splitSentences(text) {
  const parts = (text.match(/[^.!?]+[.!?]+["')\]]*\s*|[^.!?]+$/g) || [text]).map((s) => s.trim()).filter(Boolean);
  const out = [];
  for (const p of parts) {                     // merge very short bits so each request is worth it
    if (out.length && (out[out.length - 1].length < 40 || p.length < 15)) out[out.length - 1] += " " + p;
    else out.push(p);
  }
  return out;
}
function speakClone(text) {
  text = (text || "").trim();
  if (!text) return;
  if (avatarSession) { speakAvatar(text); return; }   // her live face speaks it (video + voice, lip-synced)
  for (const sentence of splitSentences(text)) {
    // all sentences start generating at once; they play in order
    speakQueue.push(apiJson("/api/speak", { text: sentence }).then((d) => ({
      blob: new Blob([Uint8Array.from(atob(d.audio_base64), (c) => c.charCodeAt(0))], { type: "audio/mpeg" }),
      chars: d.chars, starts: d.starts, ends: d.ends,
    })).catch((err) => { setStatus("Voice error: " + err.message, "error"); return null; }));
  }
  if (!speakingNow) playQueue();
}
function stopSpeaking() {
  speakQueue.length = 0;
  const a = $("cloneAudio"); a.pause(); a.onended && a.onended();
}
async function playQueue() {
  speakingNow = true;
  while (speakQueue.length) {
    const clip = await speakQueue.shift();
    if (!clip) continue;
    const a = $("cloneAudio");                  // plays directly: always audible
    a.volume = 1;
    a.src = URL.createObjectURL(clip.blob);
    if (window.TwinAvatar) TwinAvatar.speakWith(a, clip.chars, clip.starts, clip.ends);   // lip-sync timings
    setMic(false);                              // the mic must not hear the twin
    ttsSpeaking = true; setTalking(true);       // video-loop mode: show the talking clip
    try { await a.play(); }
    catch (err) { setStatus("Click anywhere on the page once to allow sound, then ask again.", "error"); }
    await new Promise((resolve) => { if (a.paused) return resolve(); a.onended = a.onerror = resolve; });
    ttsSpeaking = false; setTalking(false);
    if (window.TwinAvatar) TwinAvatar.stop();
    URL.revokeObjectURL(a.src);
  }
  setMic(true);
  speakingNow = false;
}

/* ---------- live WebRTC session ---------- */
function sendEvent(ev) { if (dc && dc.readyState === "open") dc.send(JSON.stringify(ev)); }

async function startLive() {
  if (live || pc) return;
  $("startBtn").disabled = true;
  setStatus("Connecting…");
  try {
    await ensureAudio();
    const avatarReady = ensureLiveAvatar();             // her face connects in parallel
    const { value: ephemeralKey } = await apiJson("/api/session", {});

    pc = new RTCPeerConnection();
    pc.ontrack = (e) => { $("twinAudio").srcObject = e.streams[0]; if (!CLONE) watchStream(e.streams[0]); };
    pc.onconnectionstatechange = () => {
      if (pc && ["failed", "disconnected", "closed"].includes(pc.connectionState)) stopLive("Disconnected");
    };
    micStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    micStream.getTracks().forEach((t) => pc.addTrack(t, micStream));

    dc = pc.createDataChannel("oai-events");
    dc.onmessage = (e) => { try { handleEvent(JSON.parse(e.data)); } catch (err) { console.error(err); } };
    dc.onopen = async () => {
      await avatarReady;                                 // greet only once her face is on screen
      live = true;
      setStatus("Live: speak any time", "live");
      $("startBtn").hidden = true; $("stopBtn").hidden = false;
      sendEvent({ type: "response.create", response: {
        instructions: "Greet the student warmly in one short sentence as DIGITALTWIN, their teacher's AI twin, and ask what they want to learn today." } });
    };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const r = await fetch("https://api.openai.com/v1/realtime/calls", {
      method: "POST", body: offer.sdp,
      headers: { Authorization: `Bearer ${ephemeralKey}`, "Content-Type": "application/sdp" },
    });
    if (!r.ok) throw new Error("Realtime connect failed: " + (await r.text()).slice(0, 200));
    await pc.setRemoteDescription({ type: "answer", sdp: await r.text() });
  } catch (err) {
    stopLive();
    setStatus("Error: " + err.message, "error");
  } finally {
    $("startBtn").disabled = false;
  }
}

function stopLive(msg = "Not connected") {
  live = false;
  try { dc && dc.close(); } catch (e) {}
  try { pc && pc.close(); } catch (e) {}
  if (micStream) micStream.getTracks().forEach((t) => t.stop());
  pc = dc = micStream = null;
  stopSpeaking();
  $("subtitle").textContent = "";
  $("startBtn").hidden = false; $("stopBtn").hidden = true;
  setStatus(msg);
}

function handleEvent(ev) {
  switch (ev.type) {
    case "input_audio_buffer.speech_started":       // student started talking: stop her voice (barge-in)
      if (CLONE) stopSpeaking();
      if (avatarSession) { avatarSpeakQueue.length = 0; api("/api/avatar/interrupt", { session_id: avatarSession.id }).catch(() => {}); }
      break;
    case "conversation.item.input_audio_transcription.completed":
      if (ev.transcript && ev.transcript.trim()) addMsg("student", ev.transcript.trim());
      break;
    case "response.created":
      $("subtitle").textContent = "";
      break;
    case "response.output_audio_transcript.delta":  // OpenAI preset voice
    case "response.output_text.delta":              // cloned voice (text only)
      $("subtitle").textContent += ev.delta || "";
      break;
    case "response.done":
      onResponseDone(ev.response);
      break;
    case "error":
      console.error("Realtime error", ev);
      setStatus("Realtime error: " + ((ev.error && ev.error.message) || "unknown"), "error");
      break;
  }
}

async function onResponseDone(resp) {
  const output = (resp && resp.output) || [];
  const said = output.filter((i) => i.type === "message")
    .flatMap((i) => i.content || []).map((c) => c.transcript || c.text || "").join(" ").trim();
  const calls = output.filter((i) => i.type === "function_call" && i.name === "ask_teacher" && !handledCalls.has(i.call_id));

  if (CLONE && said) speakClone(said);             // filler or final answer, in her voice
  if (!calls.length) {
    if (said && !answerShown) { addMsg("twin", said); remember("assistant", said); }
    answerShown = false;
    return;
  }
  for (const call of calls) {
    handledCalls.add(call.call_id);
    let question = "";
    try { question = JSON.parse(call.arguments || "{}").question || ""; } catch (e) {}
    const res = question ? await askBrain(question) : null;
    const toolOutput = res
      ? { speech: res.speech, from_notes: res.from_notes, chapter: res.chapter_title }
      : { error: "The answer could not be fetched. Apologise briefly and ask the student to try again." };
    sendEvent({ type: "conversation.item.create", item: { type: "function_call_output", call_id: call.call_id, output: JSON.stringify(toolOutput) } });
  }
  answerShown = true;
  sendEvent({ type: "response.create" });
}

/* ---------- typed questions ---------- */
function speakWithBrowser(text) {
  if (!("speechSynthesis" in window) || !text) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.onstart = () => { ttsSpeaking = true; setTalking(true); ensureAudio(); };
  u.onend = u.onerror = () => { ttsSpeaking = false; setTalking(false); };
  speechSynthesis.speak(u);
}

async function sendText(e) {
  e.preventDefault();
  ensureAudio();                                // unlock sound while we're still inside the click
  const q = $("textInput").value.trim();
  if (!q) return;
  $("textInput").value = "";
  addMsg("student", q);
  if (live) {
    sendEvent({ type: "conversation.item.create", item: { type: "message", role: "user", content: [{ type: "input_text", text: q }] } });
    sendEvent({ type: "response.create" });
  } else {
    const avatarReady = ensureLiveAvatar();
    const res = await askBrain(q);
    await avatarReady;
    if (res && $("speakToggle").checked) {
      if (CLONE) speakClone(res.speech); else speakWithBrowser(res.speech);
    }
  }
}

/* ---------- boot ---------- */
async function boot() {
  $("startBtn").addEventListener("click", startLive);
  $("stopBtn").addEventListener("click", () => stopLive());
  $("textForm").addEventListener("submit", sendText);
  window.addEventListener("pagehide", stopLiveAvatar);   // close the paid avatar session when the page closes
  initAvatar();
  try {
    const cfg = await apiJson("/api/config");
    CLONE = cfg.voice === "elevenlabs";
    AVATAR_LIVE = cfg.avatar === "lemonslice" && CLONE;
    TEACHER = cfg.teacherName || TEACHER;
    $("teacherName").textContent = TEACHER;
    $("subject").textContent = cfg.subject || "Physics";
    $("voiceMode").textContent = CLONE ? `🔊 Voice: ${TEACHER}'s cloned voice` : "🔊 Voice: standard AI voice";
  } catch (err) {
    setStatus("Backend not reachable at " + CFG.apiBase + " - is uvicorn running?", "error");
  }
}
boot();
