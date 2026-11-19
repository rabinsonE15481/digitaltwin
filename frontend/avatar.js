/* DIGITALTWIN photo avatar - free, runs in the browser.
 * 1. Loads her photo (media/face.jpg) and finds her mouth with MediaPipe Face Landmarker (free, in-browser).
 * 2. Every frame: draws the photo, opens the mouth (dark mouth inside + lower lip/jaw moved down),
 *    plus gentle head movement.
 * 3. Lip-sync: with her ElevenLabs voice we get the timing of every character -> mouth shapes (visemes).
 *    With other voices it follows the loudness of the audio.
 * app.js talks to it through window.TwinAvatar.
 */
const CFG = window.TWIN_CONFIG || {};
const MP = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14";
const MODEL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task";

const S = {
  ready: false, canvas: null, ctx: null, src: null, jaw: null, img: null,
  mouth: null,                 // {cx, lineY, w, chinY} in canvas pixels
  open: 0, width: 1,           // current (smoothed) mouth shape
  level: 0,                    // loudness 0..1 (non-ElevenLabs voices)
  timeline: null, audio: null, // ElevenLabs character timings + the <audio> playing them
  t0: performance.now(),
};

/* ---------- mouth shapes ---------- */
function viseme(c) {
  c = (c || "").toLowerCase();
  if (c === "a") return [1.0, 1.0];
  if (c === "e") return [0.75, 1.05];
  if (c === "i" || c === "y") return [0.6, 1.1];
  if (c === "o") return [0.85, 0.75];
  if (c === "u" || c === "w") return [0.6, 0.65];
  if ("mbp".includes(c)) return [0.0, 0.95];
  if ("fv".includes(c)) return [0.2, 1.0];
  if (/[a-z]/.test(c)) return [0.45, 1.0];
  return [0.0, 1.0];                          // space / punctuation = closed
}
function targetShape() {
  const tl = S.timeline, a = S.audio;
  if (tl && a && !a.paused) {
    const t = a.currentTime;
    let lo = 0, hi = tl.starts.length - 1, i = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (tl.starts[mid] <= t) { i = mid; lo = mid + 1; } else hi = mid - 1; }
    if (i >= 0 && t <= (tl.ends[i] || tl.starts[i]) + 0.06) return viseme(tl.chars[i]);
    return [0, 1];
  }
  return [Math.min(1, S.level * 6), 1];
}

/* ---------- setup ---------- */
function coverRect(iw, ih, cw, ch) {
  const s = Math.max(cw / iw, ch / ih);
  return { s, dx: (cw - iw * s) / 2, dy: (ch - ih * s) / 2 };
}
function loadImage(url) {
  return new Promise((res, rej) => { const i = new Image(); i.crossOrigin = "anonymous"; i.onload = () => res(i); i.onerror = rej; i.src = url; });
}
async function findMouth(img) {
  try {
    const { FaceLandmarker, FilesetResolver } = await import(`${MP}/vision_bundle.mjs`);
    const vision = await FilesetResolver.forVisionTasks(`${MP}/wasm`);
    const fl = await FaceLandmarker.createFromOptions(vision, { baseOptions: { modelAssetPath: MODEL }, runningMode: "IMAGE", numFaces: 1 });
    const lm = fl.detect(img).faceLandmarks[0];
    fl.close();
    if (!lm) return null;
    const P = (k) => ({ x: lm[k].x * img.naturalWidth, y: lm[k].y * img.naturalHeight });
    const L = P(61), R = P(291), up = P(13), lo = P(14), chin = P(152);
    return { cx: (L.x + R.x) / 2, lineY: (up.y + lo.y) / 2, w: Math.hypot(R.x - L.x, R.y - L.y), chinY: chin.y };
  } catch (e) {
    console.warn("Face detection failed, using config.mouth / default position", e);
    return null;
  }
}

async function init() {
  if ((CFG.avatarMode || "photo") !== "photo") return;
  const box = document.querySelector(".avatar");
  let img;
  try { img = await loadImage(CFG.facePhoto || "media/face.jpg"); }
  catch (e) { console.warn("No media/face.jpg - photo avatar off"); return; }

  // mouth position in photo pixels: auto-detected, or config.mouth (fractions of the photo), or a default guess
  let m = await findMouth(img);
  if (!m) {
    const f = CFG.mouth || { x: 0.5, y: 0.62, w: 0.16, chin: 0.74 };
    m = { cx: f.x * img.naturalWidth, lineY: f.y * img.naturalHeight, w: f.w * img.naturalWidth, chinY: (f.chin || f.y + 0.12) * img.naturalHeight };
  }

  const canvas = document.getElementById("faceCanvas");
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const cw = Math.round(box.clientWidth * dpr), ch = Math.round(box.clientHeight * dpr);
  canvas.width = cw; canvas.height = ch;
  const r = coverRect(img.naturalWidth, img.naturalHeight, cw, ch);

  const src = document.createElement("canvas"); src.width = cw; src.height = ch;   // photo at canvas size
  src.getContext("2d").drawImage(img, r.dx, r.dy, img.naturalWidth * r.s, img.naturalHeight * r.s);

  S.mouth = { cx: r.dx + m.cx * r.s, lineY: r.dy + m.lineY * r.s, w: m.w * r.s, chinY: r.dy + m.chinY * r.s };
  S.canvas = canvas; S.ctx = canvas.getContext("2d"); S.src = src; S.jaw = document.createElement("canvas");
  S.ready = true;
  document.body.classList.add("photoavatar");
  requestAnimationFrame(frame);
}

/* ---------- drawing ---------- */
function frame(now) {
  const { ctx, canvas, src, mouth: M } = S;
  const [tOpen, tWidth] = targetShape();
  S.open += (tOpen - S.open) * 0.35;
  S.width += (tWidth - S.width) * 0.3;
  const strength = CFG.mouthStrength || 1;

  // gentle head movement (more while talking)
  const t = (now - S.t0) / 1000, talk = S.open > 0.05 ? 1 : 0.4;
  const pivotY = M.chinY + (M.chinY - M.lineY) * 2;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.save();
  ctx.translate(M.cx, pivotY);
  ctx.rotate(Math.sin(t * 0.9) * 0.006 * talk);
  ctx.translate(-M.cx, -pivotY + Math.sin(t * 1.3) * 1.2 * talk);
  ctx.drawImage(src, 0, 0);

  const h = S.open * M.w * 0.28 * strength;     // how far the mouth opens (pixels)
  if (h > 0.6) {
    const mw = M.w * 0.92 * S.width;
    const L = M.cx - mw / 2, R = M.cx + mw / 2, y = M.lineY;
    // inside of the mouth: a soft lens shape between the lips
    ctx.save();
    ctx.filter = "blur(0.8px)";
    ctx.fillStyle = "rgb(58,20,22)";
    ctx.beginPath();
    ctx.moveTo(L, y);
    ctx.quadraticCurveTo(M.cx, y - h * 0.15, R, y);
    ctx.quadraticCurveTo(M.cx, y + h * 1.9, L, y);
    ctx.fill();
    if (h > M.w * 0.1) {                         // a hint of upper teeth
      ctx.fillStyle = "rgba(214,204,194,0.59)";
      ctx.beginPath();
      ctx.ellipse(M.cx, y - 1 + (h * 0.28 + 1) / 2, mw * 0.3, (h * 0.28 + 1) / 2, 0, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
    // lower lip + jaw moved down by h, feathered so it blends into the cheeks and chin
    const jw = M.w * 2.0, rh = (M.chinY - M.lineY) * 1.35;
    const J = S.jaw; J.width = Math.ceil(jw); J.height = Math.ceil(rh + h + 1);
    const jc = J.getContext("2d");
    jc.clearRect(0, 0, J.width, J.height);
    jc.drawImage(src, M.cx - jw / 2, M.lineY, jw, rh, 0, h, jw, rh);
    jc.globalCompositeOperation = "destination-in";
    jc.save();
    const rx = jw / 2, ry = rh * 0.52;
    jc.translate(jw / 2, h + rh * 0.5); jc.scale(1, ry / rx);
    const g = jc.createRadialGradient(0, 0, 0, 0, 0, rx);
    g.addColorStop(0, "rgba(0,0,0,1)"); g.addColorStop(0.75, "rgba(0,0,0,1)"); g.addColorStop(1, "rgba(0,0,0,0)");
    jc.fillStyle = g; jc.fillRect(-rx * 2, -rx * 2, rx * 4, rx * 4);
    jc.restore();
    jc.globalCompositeOperation = "source-over";
    ctx.drawImage(J, M.cx - jw / 2, M.lineY);
  }
  ctx.restore();

  if (CFG.avatarDebug) {                         // shows where it thinks the mouth and chin are
    ctx.strokeStyle = "lime"; ctx.lineWidth = 2;
    ctx.strokeRect(M.cx - M.w / 2, M.lineY - 2, M.w, 4);
    ctx.beginPath(); ctx.moveTo(M.cx - 20, M.chinY); ctx.lineTo(M.cx + 20, M.chinY); ctx.stroke();
  }
  requestAnimationFrame(frame);
}

/* ---------- API for app.js ---------- */
window.TwinAvatar = {
  get ready() { return S.ready; },
  get state() { return { open: +S.open.toFixed(2), timeline: !!S.timeline, playing: !!(S.audio && !S.audio.paused) }; },
  setLevel(v) { S.level = v; },                                  // loudness 0..1
  speakWith(audioEl, chars, starts, ends) {                     // ElevenLabs timings for this clip
    S.audio = audioEl; S.timeline = chars && chars.length ? { chars, starts, ends } : null;
  },
  stop() { S.timeline = null; S.audio = null; },
};
init();
