"""AskDoc - read-only diagnostic for stray characters in Sources.

Changes nothing. Run from your AskDoc project folder:

    python diagnose_sources.py

It checks two stages and tells you where the stray characters come from:
  Stage 1  raw text extracted from your original uploaded PDFs (uploads/)
  Stage 2  the chunks stored in the index (vectorstore/)
"""

import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent

CHECKS = {
    "word 'svg'": re.compile(r"\bsvg\b", re.IGNORECASE),
    "private-use icon glyph (U+E000-U+F8FF)": re.compile("[-]"),
    "literal **{** or **}**": re.compile(r"\*\*[{}]\*\*"),
}


def scan(text):
    """Return {check name: [short context snippets]} for one piece of text."""
    found = {}
    for name, rx in CHECKS.items():
        snippets = []
        for match in rx.finditer(text):
            start = max(0, match.start() - 25)
            end = min(len(text), match.end() + 25)
            snippets.append(repr(text[start:end]))
        if snippets:
            found[name] = snippets
    return found


def report(title, items):
    """items: list of (label, text). Prints a summary, returns flagged count."""
    flagged = 0
    examples = {}
    counts = {name: 0 for name in CHECKS}

    for label, text in items:
        hits = scan(text)
        if hits:
            flagged += 1
        for name, snippets in hits.items():
            counts[name] += len(snippets)
            examples.setdefault(name, (label, snippets[0]))

    print(f"{title}: {len(items)} text pieces scanned, {flagged} contain stray characters")
    for name, total in counts.items():
        if total:
            label, snippet = examples[name]
            print(f"   - {name}: {total} occurrence(s). Example in {label}: {snippet}")
    return flagged


# ---------------- Stage 1: raw PDF text ----------------
print("=== Stage 1: raw text from original PDFs in uploads/ ===")
raw_items = []
pdfs = sorted(BASE.glob("uploads/*/*/*.pdf"))

if not pdfs:
    print("No PDFs found in uploads/ (skipping this stage).")
else:
    try:
        from pypdf import PdfReader

        for pdf in pdfs:
            try:
                reader = PdfReader(str(pdf))
                for number, page in enumerate(reader.pages, start=1):
                    raw_items.append((f"{pdf.name} p{number}", page.extract_text() or ""))
            except Exception as exc:
                print(f"Could not read {pdf.name}: {exc}")
    except ImportError:
        print("pypdf is not installed here (skipping this stage).")

raw_flagged = report("PDF extraction", raw_items) if raw_items else 0

# ---------------- Stage 2: stored chunks ----------------
print("\n=== Stage 2: chunks stored in the index (vectorstore/) ===")
chunk_items = []

for meta_file in sorted(BASE.glob("vectorstore/*/*/qa_metadata.json")):
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Could not read {meta_file}: {exc}")
        continue

    for chunk, meta in zip(data.get("chunks", []), data.get("metadata", [])):
        label = f"{meta.get('source', '?')} chunk {meta.get('chunk', '?')}"
        chunk_items.append((label, chunk))

if not chunk_items:
    print("No stored chunks found.")
chunk_flagged = report("Stored chunks", chunk_items) if chunk_items else 0

# ---------------- Verdict ----------------
print("\n=== Verdict ===")
if chunk_flagged or raw_flagged:
    print("The stray characters ARE in the document text itself (PDF extraction).")
    print("Tell Claude the output above; the fix goes in rag.py extraction, not in the UI.")
elif chunk_items:
    print("The stored text is CLEAN. Nothing in the PDF text or the index contains them.")
    print("So they come from how the Sources panel is drawn (st.json), not from your data.")
else:
    print("Nothing to scan yet. Upload a document first, then run this again.")
