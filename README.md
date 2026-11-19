# DIGITALTWIN: AI twin of an IHS teacher

DIGITALTWIN is a live, talking AI version of a teacher at The Indian High Group of Schools.
Students speak to it (WebRTC live voice) or type. It answers **her subject** (Physics: NCERT Class 11 notes)
and **general knowledge / study questions**, in her style and **her cloned voice** (ElevenLabs), with her face on screen.

## How it works

```
Student (browser) ──WebRTC──► OpenAI Realtime (listens, turn-taking, writes text)
      │                            │ calls tool "ask_teacher"
      └─HTTPS──► Backend (FastAPI on Lambda)
                   /api/ask    Luna: subject chapter / general / unsuitable
                               Sol:  answer from the chapter notes, or general knowledge
                   /api/speak  ElevenLabs: text -> her cloned voice (sentence by sentence)
Avatar: her photo (frontend/media/face.jpg) comes alive in the browser: lips move in sync with her
        voice using ElevenLabs character timings (avatar.js). Set avatarMode: "video" in config.js for video loops.
```

- 📘 badge = from the class notes · 🤖 badge = general knowledge.
- No class code. **Set an OpenAI budget limit and ElevenLabs usage alerts**, and only share the link with students.
- Without ElevenLabs keys in `.env`, the app uses OpenAI's standard voice.

## Folder map

```
Digitaltwin/
  backend/            Lambda app (FastAPI + Mangum)
    main.py           API routes: /api/session, /api/ask, /api/health
    brain.py          Luna routing + Sol answering + all prompts
    persona.md        ← EDIT: how the teacher talks
    chapters.json     chapter list + topics (used for routing)
    kb/               ch1.md … ch7.md notes (created by extract.py)
    Dockerfile, requirements.txt, .env.example
  frontend/           static website (no build step)
    index.html, app.js, style.css
    config.js         ← EDIT: API URL + teacher name
    media/            face.jpg, idleloop.mp4, talkloop.mp4 (from makeloops.ps1)
  scripts/
    extract.py        PDFs → clean Markdown notes (equations as LaTeX)
    makeloops.ps1     her video → avatar loops + voice sample
    deploy.ps1        Docker → ECR → Lambda + Function URL
```

---

## Step-by-step

### Step 0: Install once (Windows)
- Python 3.12, **Docker Desktop** (running), **AWS CLI v2**, ffmpeg (`winget install ffmpeg`)
- `aws configure` → your access key, secret, region `me-central-1`
- OpenAI dashboard → **Billing → set a monthly budget limit** (e.g. $20)

Unzip the project, open PowerShell in the `Digitaltwin` folder:
```powershell
python -m venv venv
venv\Scripts\activate
pip install -r scripts\requirements.txt -r backend\requirements.txt
copy backend\.env.example backend\.env
notepad backend\.env          # OpenAI key, TEACHER_NAME, SUBJECT, ElevenLabs key + voice ID
```
Check the exact model names your key can use, and fix them in `.env` if they differ:
```powershell
python -c "from dotenv import load_dotenv; load_dotenv('backend/.env'); from openai import OpenAI; print(sorted(m.id for m in OpenAI().models.list()))"
```

### Step 1: Check the PDFs (free, no API calls)
```powershell
python scripts\extract.py --src "C:\Users\OMC-LP185\Downloads\PHYSICS" --check
```
It lists every chapter with its page count and warns about wrong or duplicate files.
⚠️ In your folder, **CH-6 is exactly the same size as CH-1 (1,106 KB)**, but Chapter 6 should be the biggest one (35 pages).
It is probably a duplicate download. If the check flags it, download Chapter 6 again from the NCERT site.

### Step 2: Turn the PDFs into notes (about 15 min, about $2 one-time)
```powershell
python scripts\extract.py --src "C:\Users\OMC-LP185\Downloads\PHYSICS"
```
This creates `backend\kb\ch1.md … ch7.md`. If it stops, run it again; it resumes from where it stopped.
**Open 2-3 files and spot-check them**: equations readable? Examples complete?
You can also add the teacher's own tips or notes at the end of any chapter file.

### Step 3: Fill in her persona
Edit `backend\persona.md` using her video and lesson recordings: greeting, favourite phrases,
how she explains, how she corrects mistakes. **This is what makes it sound like her.**

### Step 4: Avatar from her 1-minute video
Watch the video and note a stretch where she is **talking** and a stretch where she is **quiet or nodding**:
```powershell
.\scripts\makeloops.ps1 -Video "C:\path\teacher.mp4" -TalkStart 00:00:12 -TalkSeconds 8 -IdleStart 00:00:45 -IdleSeconds 6
```
This creates `frontend\media\face.jpg`, `talkloop.mp4` and `idleloop.mp4`, plus `voicesample.wav` (kept for Version 2).

### Step 5: Test locally
Terminal 1 (backend):
```powershell
cd backend
uvicorn main:app --port 8000
```
Open http://localhost:8000/api/health. All 7 chapters should show `true`.

Terminal 2 (frontend):
```powershell
cd frontend
python -m http.server 5500
```
Open **http://localhost:5500**, then:
1. Type: *"What is the difference between distance and displacement?"* → expect a 📘 Ch 2 answer.
2. Click **Start live class**, allow the microphone, and ask by voice: *"Explain Newton's second law."*
3. Ask a follow-up: *"Give me one example."*
4. Ask general knowledge: *"Which is the largest planet?"* → expect a 🤖 answer in her voice.

### Step 6: Deploy the backend to AWS (Docker → ECR → Lambda)
```powershell
.\scripts\deploy.ps1 -Tag v1
```
The first run creates the ECR repo, IAM role, Lambda (1 GB RAM, 60 s timeout) and a public Function URL.
It prints the **API URL**. Open `API URL/api/health` to confirm it works.
Each later update: `.\scripts\deploy.ps1 -Tag v2` (always use a new tag).

### Step 7: Host the frontend (HTTPS is required for the microphone)
1. Put the API URL into `frontend\config.js` (`apiBase`, no trailing slash) and set `teacherName`.
2. Zip the **contents** of the `frontend` folder.
3. AWS Console → **Amplify** → *Create new app* → **Deploy without Git** → upload the zip.
   You get an `https://….amplifyapp.com` link.
4. Put that link in `backend\.env` → `ALLOWED_ORIGINS=https://….amplifyapp.com`, then run `.\scripts\deploy.ps1 -Tag v2`.

### Step 8: Pilot
- Run 30 test questions: 20 from the chapters, 5 outside the syllabus, 5 follow-ups.
- Have the teacher review 20 answers for **tone** and fix `persona.md` (then redeploy).
- To see which questions the notes didn't cover: CloudWatch → Log groups → `/aws/lambda/digitaltwin-brain` →
  search `"from_notes": false`. Add those topics to the notes.

---

## Costs (approximate)
| Part | Cost |
|---|---|
| Notes extraction (one-time) | about $2 |
| Typed question (Luna + Sol, chapter context) | about $0.01-0.04 |
| Live voice (OpenAI Realtime) | the main cost: billed per minute of audio, so watch usage |
| Lambda, ECR, Amplify | free tier / a few cents |

## Troubleshooting
| Problem | Fix |
|---|---|
| `Voice error` | Check `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` in `.env`, then restart the backend |
| `Model error: … model not found` | Fix the model IDs in `.env` (see Step 0), then redeploy |
| `OpenAI realtime error` on Start | Check `REALTIME_MODEL`, `REALTIME_VOICE`, `TRANSCRIBE_MODEL` in `.env`; check the Realtime API is enabled for your key |
| CORS error in the browser console | `ALLOWED_ORIGINS` must contain the exact site URL. Do **not** enable CORS on the Function URL itself |
| Function URL gives 403 | Lambda console → your function → Configuration → Function URL → check auth type NONE and the resource-based policy |
| Microphone blocked | The site must be on `https://` or `localhost` |
| Answers are slow | Set `REASONING_EFFORT=low` (default). Chapter 6 is the largest, so it is slightly slower |
| Avatar doesn't switch | Check the media files exist; the photo fallback pulses instead |

## Version 2 ideas
- **Live lip-sync**: a streaming avatar service that accepts our ElevenLabs audio
- **Intro clip**: ElevenLabs Avatar video of her, played when the app opens
- Physics Part II chapters: add PDFs → run extract.py → add them to chapters.json
- Step-by-step drawing videos for numericals, quiz mode, teacher admin page
