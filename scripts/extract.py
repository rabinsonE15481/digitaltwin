"""Convert the NCERT Physics chapter PDFs into clean Markdown notes (backend/kb/ch1.md ... ch7.md).

Each page is rendered as an image and read by an OpenAI vision model, so equations come out as LaTeX.
Pages are cached in extractcache/ - if it stops halfway, just run it again and it resumes.

  Check the PDFs only (free, no API calls):
    python scripts\\extract.py --src "C:\\Users\\OMC-LP185\\Downloads\\PHYSICS" --check
  Extract everything:
    python scripts\\extract.py --src "C:\\Users\\OMC-LP185\\Downloads\\PHYSICS"
  Re-do only some chapters:
    python scripts\\extract.py --src "..." --only 6 7 --force
"""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHAPTERS = {c["n"]: c for c in json.loads((ROOT / "backend" / "chapters.json").read_text(encoding="utf-8"))}

PROMPT = """You are converting ONE page of the NCERT Class 11 Physics textbook (Part I) into clean Markdown
for a teaching assistant's knowledge base.
Rules:
- Keep all text faithfully, in reading order. Do not summarise, explain or add anything.
- Keep section headings with their numbers as Markdown headings, e.g. "## 4.3 NEWTON'S SECOND LAW OF MOTION".
- Write every equation, variable and unit expression in LaTeX: inline $...$, display $$...$$.
- Mark worked examples as "**Example 4.2**" and keep the full question and solution.
- Convert tables to Markdown tables.
- For each figure write one line: [FIGURE Fig. 4.3: short description of what it shows].
- Skip running headers, footers, page numbers and decorative elements.
- Output only the Markdown."""


def _fitz():
    try:
        import pymupdf as fitz
    except ImportError:  # older PyMuPDF versions
        import fitz
    return fitz


def find_pdfs(src: pathlib.Path) -> dict:
    found = {}
    for pdf in sorted(src.glob("*.pdf")):
        m = re.search(r"CH[-_ ]?(\d+)", pdf.stem, re.IGNORECASE)
        if m and int(m.group(1)) in CHAPTERS:
            found[int(m.group(1))] = pdf
    return found


def check(found: dict) -> bool:
    fitz = _fitz()

    ok = True
    missing = sorted(set(CHAPTERS) - set(found))
    if missing:
        print(f"!! Missing chapters: {missing}")
        ok = False
    seen = {}
    for n, pdf in sorted(found.items()):
        digest = hashlib.md5(pdf.read_bytes()).hexdigest()
        pages = len(fitz.open(pdf))
        expected = CHAPTERS[n]["pages"]
        first = fitz.open(pdf)[0].get_text().strip().replace("\n", " ")[:70]
        flag = ""
        if digest in seen:
            flag = f"  !! IDENTICAL to chapter {seen[digest]} - wrong file downloaded?"
            ok = False
        elif abs(pages - expected) > 2:
            flag = f"  !! expected about {expected} pages"
            ok = False
        seen.setdefault(digest, n)
        print(f"Ch {n}: {pdf.name:35s} {pages:3d} pages | starts: {first!r}{flag}")
    return ok


def page_to_markdown(client, model, png: bytes) -> str:
    b64 = base64.b64encode(png).decode()
    kwargs = dict(
        model=model,
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": PROMPT},
            {"type": "input_image", "image_url": f"data:image/png;base64,{b64}"},
        ]}],
    )
    for attempt in range(5):
        try:
            return client.responses.create(**kwargs).output_text.strip()
        except Exception as exc:  # rate limits / network blips: back off and retry
            wait = 5 * (attempt + 1)
            print(f"    retry in {wait}s ({str(exc)[:120]})")
            time.sleep(wait)
    raise RuntimeError("Page failed 5 times - run the script again to resume.")


def extract(found: dict, out: pathlib.Path, model: str, dpi: int, force: bool):
    fitz = _fitz()
    from openai import OpenAI

    client = OpenAI()
    cache_root = ROOT / "extractcache"
    out.mkdir(parents=True, exist_ok=True)
    for n, pdf in sorted(found.items()):
        target = out / f"ch{n}.md"
        if target.exists() and not force:
            print(f"Ch {n}: {target.name} already exists (use --force to redo)")
            continue
        cache = cache_root / f"ch{n}"
        cache.mkdir(parents=True, exist_ok=True)
        doc = fitz.open(pdf)
        parts = [f"# Chapter {n}: {CHAPTERS[n]['title']}\n"]
        for i, page in enumerate(doc, start=1):
            cached = cache / f"p{i}.md"
            if cached.exists() and not force:
                md = cached.read_text(encoding="utf-8")
            else:
                print(f"Ch {n}: page {i}/{len(doc)}")
                md = page_to_markdown(client, model, page.get_pixmap(dpi=dpi).tobytes("png"))
                cached.write_text(md, encoding="utf-8")
            parts.append(f"\n<!-- page {i} -->\n{md}\n")
        target.write_text("".join(parts), encoding="utf-8")
        print(f"Ch {n}: saved {target} ({target.stat().st_size // 1024} KB)")


def main():
    try:  # pick up OPENAI_API_KEY from backend/.env if present
        from dotenv import load_dotenv
        load_dotenv(ROOT / "backend" / ".env")
    except ImportError:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="Folder with PHYSICS-GRADE11-CH-N.pdf files")
    ap.add_argument("--out", default=str(ROOT / "backend" / "kb"))
    ap.add_argument("--model", default=os.getenv("EXTRACT_MODEL", os.getenv("ANSWER_MODEL", "gpt-6.1-sol")))
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--only", type=int, nargs="*", help="Only these chapter numbers")
    ap.add_argument("--force", action="store_true", help="Redo even if output/cache exists")
    ap.add_argument("--check", action="store_true", help="Only check the PDFs, no API calls")
    ap.add_argument("--ignore-problems", action="store_true", help="Extract even if the check finds problems")
    args = ap.parse_args()

    found = find_pdfs(pathlib.Path(args.src))
    print(f"Found {len(found)} chapter PDFs in {args.src}\n")
    ok = check(found)
    if args.check:
        print("\nCheck passed." if ok else "\nFix the problems above, then run again.")
        return
    if not ok and not args.ignore_problems:
        sys.exit("\nStopping: fix the PDF problems above first (or pass --ignore-problems).")
    if args.only:
        found = {n: p for n, p in found.items() if n in args.only}
    extract(found, pathlib.Path(args.out), args.model, args.dpi, args.force)
    print("\nDone. Open the .md files and spot-check a few pages (equations, examples).")


if __name__ == "__main__":
    main()
