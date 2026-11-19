"""DIGITALTWIN brain.

Step 1 - Luna (cheap, fast) decides: is this about one of her subject chapters, general knowledge,
         or something not suitable for school?
Step 2 - Sol (smart) answers in the teacher's style: from the chapter notes when they cover it,
         otherwise from general knowledge at a school-student level.
"""
import json
import os
import pathlib
from functools import lru_cache

from openai import BadRequestError, OpenAI

BASE = pathlib.Path(__file__).parent
ROUTER_MODEL = os.getenv("ROUTER_MODEL", "gpt-6-luna")
ANSWER_MODEL = os.getenv("ANSWER_MODEL", "gpt-6.1-sol")
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "low").strip()
TEACHER_NAME = os.getenv("TEACHER_NAME", "Malathy ma'am")
SUBJECT = os.getenv("SUBJECT", "Physics")

client = OpenAI()
CHAPTERS = json.loads((BASE / "chapters.json").read_text(encoding="utf-8"))
CHAPTER_BY_N = {c["n"]: c for c in CHAPTERS}
PERSONA = ((BASE / "persona.md").read_text(encoding="utf-8")
           .replace("{TEACHER_NAME}", TEACHER_NAME).replace("{SUBJECT}", SUBJECT))

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "chapter": {"type": "integer", "description": "Subject chapter number that covers it, or 0 if none"},
        "category": {"type": "string", "enum": ["subject", "general", "unsuitable"],
                     "description": "subject = her subject; general = any other knowledge/study/everyday question; "
                                    "unsuitable = harmful or not appropriate for school students"},
    },
    "required": ["chapter", "category"],
    "additionalProperties": False,
}

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "display": {"type": "string", "description": "Markdown answer for the screen, equations in LaTeX"},
        "speech": {"type": "string", "description": "Short spoken version, plain words, no LaTeX or symbols"},
        "from_notes": {"type": "boolean", "description": "True only if the class notes cover this"},
    },
    "required": ["display", "speech", "from_notes"],
    "additionalProperties": False,
}

ANSWER_RULES = f"""
# ANSWER RULES
- Answer the student's latest question in the teacher's style.
- If CLASS NOTES are given below and cover the question, use them as the main source and set from_notes = true.
- Otherwise answer from your own knowledge (from_notes = false). General knowledge, current-affairs-style facts,
  other school subjects, study tips and everyday questions are all welcome: explain them simply and accurately
  for a school student. For {SUBJECT} topics beyond the notes, answer at Class 11 level.
- If you are not sure, or the fact may have changed recently, say so honestly instead of guessing.
- display: Markdown for the screen. Short paragraphs or numbered steps, at most about 250 words.
  Write every equation in LaTeX ($...$ inline, $$...$$ on its own line).
  For numericals use: Given -> Formula -> Substitution -> Answer (with units).
- For textbook exercise problems, guide with hints and the method first; give the full worked answer
  only if the student asks for it.
- speech: what the twin says aloud. Natural spoken English in the teacher's style, at most about 80 words,
  no LaTeX, no symbols, no markdown. Say formulas in words ("v equals u plus a t").
  For long working, say something like "I have put the full steps on your screen."
- Mention the chapter or example number when helpful. Never invent page numbers.
"""

UNSUITABLE_NOTE = ("\nNOTE: This question is not suitable for school students. Kindly decline in one or two "
                   "sentences in both fields, without lecturing, and suggest talking to a teacher or parent "
                   "if they need help. Set from_notes = false.")

REALTIME_RULES = """
# LIVE VOICE RULES
- You are speaking live with a student by voice. Keep your turns short and natural.
- For EVERY real question (subject, general knowledge, study help, follow-ups like "why?" or "give an example"):
  1. First say one very short natural filler in her style, like "Good question, let me think."
  2. Then call the ask_teacher tool with the student's full question, rewritten so it makes sense on its own.
  3. When the tool returns, say its "speech" text naturally. Do not add new facts beyond it,
     and never read out LaTeX or symbols.
- Greetings, thanks and small talk: reply briefly yourself without the tool.
- If the student interrupts, stop and listen.
- Speak clear classroom English at a steady pace.
"""

ASK_TOOL = {
    "type": "function",
    "name": "ask_teacher",
    "description": "Get the teacher's answer to a student's question (her subject, general knowledge, "
                   "study help). Call this for every real question or follow-up.",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The student's full question, rewritten to be self-contained."}
        },
        "required": ["question"],
    },
}


@lru_cache(maxsize=None)
def chapter_notes(n: int) -> str:
    path = BASE / "kb" / f"ch{n}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _clean_history(history):
    out = []
    for turn in (history or [])[-8:]:
        role, content = turn.get("role"), str(turn.get("content", ""))[:2000]
        if role in ("user", "assistant") and content.strip():
            out.append({"role": role, "content": content})
    return out


def _call_json(model, instructions, messages, schema, name, effort=""):
    kwargs = dict(model=model, instructions=instructions, input=messages,
                  text={"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}})
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    try:
        resp = client.responses.create(**kwargs)
    except BadRequestError:
        if "reasoning" not in kwargs:
            raise
        kwargs.pop("reasoning")
        resp = client.responses.create(**kwargs)
    return json.loads(resp.output_text)


def route(question: str, history) -> dict:
    menu = "\n".join(f"{c['n']}. {c['title']}: {c['topics']}" for c in CHAPTERS)
    instructions = (
        f"You sort a school student's question for their {SUBJECT} teacher's AI twin.\n"
        f"{SUBJECT} chapters with class notes:\n{menu}\n\n"
        "Use the recent conversation to resolve follow-ups like 'why?' or 'give an example'.\n"
        "chapter = matching chapter number if one of these chapters covers it, else 0.\n"
        f"category = 'subject' for {SUBJECT}, 'general' for any other knowledge/study/everyday question, "
        "'unsuitable' only for harmful or clearly inappropriate requests."
    )
    messages = _clean_history(history)[-4:] + [{"role": "user", "content": question}]
    result = _call_json(ROUTER_MODEL, instructions, messages, ROUTE_SCHEMA, "route")
    if result.get("chapter") not in CHAPTER_BY_N:
        result["chapter"] = 0
    return result


def ask(question: str, history=None) -> dict:
    history = _clean_history(history)
    routed = route(question, history)
    category = routed.get("category", "general")
    chapter = CHAPTER_BY_N.get(routed["chapter"]) if category == "subject" else None
    notes = chapter_notes(chapter["n"]) if chapter else ""

    if notes:
        context = f"\n# CLASS NOTES - {SUBJECT} Chapter {chapter['n']}: {chapter['title']}\n\n{notes}"
    else:
        context = "\n# CLASS NOTES\nNo class notes for this question. Answer from your own knowledge."
    if category == "unsuitable":
        context += UNSUITABLE_NOTE

    instructions = PERSONA + "\n" + ANSWER_RULES + context
    messages = history + [{"role": "user", "content": question}]
    answer = _call_json(ANSWER_MODEL, instructions, messages, ANSWER_SCHEMA, "answer", REASONING_EFFORT)

    return {
        "display": answer.get("display", ""),
        "speech": answer.get("speech", ""),
        "from_notes": bool(answer.get("from_notes")) and bool(notes),
        "category": category,
        "chapter": chapter["n"] if chapter else 0,
        "chapter_title": chapter["title"] if chapter else "",
    }


def realtime_instructions() -> str:
    return PERSONA + "\n" + REALTIME_RULES
