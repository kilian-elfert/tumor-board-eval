#!/usr/bin/env python3
"""
Pre-generate highlight mappings for the Falschinformationen step.

For each case and each text source (human summary / llm summary / protocol),
this script asks an LLM to identify the exact text excerpts that correspond to
each INFO_ITEM. The result is saved to  highlight_mappings.json  which the app
loads at runtime.

Naming convention
-----------------
A case is identified by a single ``case_id`` string (currently the SHA-256
hash of the source TK protocol). Files are expected at:

    texts_human/zusammenfassung/<case_id>.txt   (human_summary)
    texts_llm/zusammenfassung/<case_id>.txt     (llm_summary)
    <DOCUMENTS_DIR>/<case_id>.txt               (protocol, optional)

``DOCUMENTS_DIR`` resolves to ``$DATA_ROOT/$SOURCES_SUBDIR`` when configured
(see ``.env`` / ``app.py``), otherwise to in-repo ``original_documents/``.
The ``protocol`` mapping is skipped silently if no plain-text protocol is
available for a case.

Usage:
    # Set your API key first (or put it in .env)
    set OPENAI_API_KEY=sk-...

    python generate_highlights.py                 # all cases, incremental
    python generate_highlights.py --case <hash>   # only this case, force rerun
    python generate_highlights.py --dry-run       # print prompts, don't call LLM
"""

import argparse
import json
import os
import re
import sys
import time

# Load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEXTS_HUMAN_DIR = os.path.join(BASE_DIR, "texts_human")
TEXTS_LLM_DIR = os.path.join(BASE_DIR, "texts_llm")
OUTPUT_FILE = os.path.join(BASE_DIR, "highlight_mappings.json")

# Original documents directory: prefer external DATA_ROOT/<SOURCES_SUBDIR>
# (mirrors app.py), fall back to in-repo 'original_documents/'.
_DATA_ROOT = os.path.expanduser(os.environ.get("DATA_ROOT", "").strip())
_SOURCES_SUBDIR = os.environ.get("SOURCES_SUBDIR", "sources").strip()
_SOURCES_DIR = os.path.join(_DATA_ROOT, _SOURCES_SUBDIR) if _DATA_ROOT else ""
if _SOURCES_DIR and os.path.isdir(_SOURCES_DIR):
    DOCUMENTS_DIR = _SOURCES_DIR
else:
    DOCUMENTS_DIR = os.path.join(BASE_DIR, "original_documents")

# Case-id heuristic (mirrors app.py): 64-char lowercase hex SHA-256 stem.
_CASE_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_case_id(s):
    return bool(_CASE_ID_RE.match(s or ""))

INFO_ITEMS = [
    "Demographie: Alter",
    "Demographie: Geschlecht",
    "Allgemeinzustand: Komorbiditäten",
    "Allgemeinzustand: Funktioneller Zustand (e.g. ECOG, Karnofsky)",
    "Primärtumor: Datum der Erstdiagnose",
    "Primärtumor: Art des Tumors",
    "Primärtumor: Lokalisation",
    "Primärtumor: Tumordicke",
    "Primärtumor: Ulzeration",
    "Primärtumor: Mitotische Aktivität",
    "Primärtumor: Stadium der Erkrankung bei Erstdiagnose",
    "Primärtumor: Mutationsstatus (e.g. BRAF)",
    "Primärtumor: PD-L1 Status",
    "Primärtherapie: Resektionsstatus",
    "Primärtherapie: Sicherheitsabstand",
    "Primärtherapie: SLNE",
    "Primärtherapie: CLND",
    "Primärtherapie: Anzahl LK entfernt",
    "Primärtherapie: Anzahl LK befallen",
    "Therapieverlauf: Strahlentherapie",
    "Therapieverlauf: Andere lokoregionäre Therapien (e.g. IL-2, T-VEC)",
    "Therapieverlauf: Systemtherapie",
    "Therapieverlauf: Therapiewechsel (Start, Stopp) und Begründung",
    "Therapieverlauf: Nebenwirkungen und Komplikationen",
    "Therapieverlauf: Aktuelle Therapie",
    "Krankheitsverlauf: Datum Erstdiagnose Lymphknotenmetastasierung (i.e., Stadium III)",
    "Krankheitsverlauf: Datum Erstdiagnose Fernmetastasen (i.e., Stadium IV)",
    "Aktuelle Befunde: Bildgebende Verfahren (e.g. CT, MRT, PET-CT, LK-Sono)",
    "Aktuelle Befunde: Laborwerte (e.g. S100, LDH, HLA-A2)",
    "Aktueller Status: Krankheitsstatus (unverändert, progredient, regredient)",
    "Aktueller Status: Stadium der Erkrankung",
    "Aktueller Status: Metastasierung",
    "Aktueller Status: Lokalisation der Metastasierung",
    "Aktueller Status: Symptome und Beschwerden",
    "Entscheidungsrelevante Faktoren: Patientenpräferenzen",
    "Entscheidungsrelevante Faktoren: Behandlungsziel",
    "Entscheidungsrelevante Faktoren: Beschluss der letzten Tumorkonferenz",
]


def slugify(label):
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in label)


# ---------------------------------------------------------------------------
# Discover cases (hash-based naming, mirrors app.py)
# ---------------------------------------------------------------------------
def _read_text_file(path):
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def discover_cases():
    """Return list of (case_id, human_summary, llm_summary, protocol_text).

    Mirrors ``app.py``'s discovery: a case is any ``<case_id>`` for which at
    least one of these files exists:
      - texts_human/zusammenfassung/<case_id>.txt
      - texts_llm/zusammenfassung/<case_id>.txt
      - <DOCUMENTS_DIR>/<case_id>.{txt,pdf}  (DOCUMENTS_DIR honors DATA_ROOT)

    The ``protocol_text`` is read from ``<DOCUMENTS_DIR>/<case_id>.txt`` when
    present; if only a PDF exists (or nothing), it will be empty and the
    protocol mapping for that case is skipped.
    """
    case_ids = set()

    for sub_dir in (
        os.path.join(TEXTS_HUMAN_DIR, "zusammenfassung"),
        os.path.join(TEXTS_LLM_DIR, "zusammenfassung"),
    ):
        if os.path.isdir(sub_dir):
            for fname in os.listdir(sub_dir):
                if fname.lower().endswith(".txt"):
                    case_ids.add(os.path.splitext(fname)[0])

    if os.path.isdir(DOCUMENTS_DIR):
        for fname in os.listdir(DOCUMENTS_DIR):
            stem, ext = os.path.splitext(fname)
            if ext.lower() in (".txt", ".pdf") and _is_case_id(stem):
                case_ids.add(stem)

    cases = []
    for case_id in sorted(case_ids):
        human = _read_text_file(
            os.path.join(TEXTS_HUMAN_DIR, "zusammenfassung", f"{case_id}.txt")
        )
        llm = _read_text_file(
            os.path.join(TEXTS_LLM_DIR, "zusammenfassung", f"{case_id}.txt")
        )
        protocol = _read_text_file(
            os.path.join(DOCUMENTS_DIR, f"{case_id}.txt")
        )
        cases.append((case_id, human, llm, protocol))
    return cases


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are a medical text analysis assistant. Given a clinical tumor board summary \
and a list of information categories, identify the EXACT text excerpts from the \
summary that correspond to each category.

Rules:
- Return a JSON object mapping each category slug to a list of exact verbatim \
  excerpts from the summary text (copy-paste precision, including original \
  whitespace, line breaks, dashes, and punctuation).
- Each excerpt should be the shortest self-contained phrase that conveys the \
  information (typically 3-30 words). Avoid single words unless that is all \
  that is present.
- If a category is NOT present in the text, map it to an empty list [].
- Do NOT paraphrase, summarize, condense, reorder, or relabel. Do NOT add \
  category labels (e.g. "Metastasierung:", "Systemtherapie:") that are not \
  literally in the source. Do NOT merge information from different lines or \
  paragraphs into one excerpt — instead, emit each contiguous source span as \
  its own list entry.
- Do NOT replace characters: keep the exact dash style (- vs ‐ vs –), the \
  exact spaces (including non-breaking spaces), and the exact line breaks.
- Every excerpt must be findable via plain substring search in the original.
- Return ONLY valid JSON, no markdown fences, no explanation.
"""

PROTOCOL_SYSTEM_PROMPT = """\
You are a medical text analysis assistant. Given an original tumor board \
protocol and a list of information categories, extract the EXACT text excerpts \
from the protocol that correspond to each category.

Rules:
- Return a JSON object mapping each category slug to a list of exact verbatim \
  excerpts from the protocol (copy-paste precision, including original \
  whitespace, line breaks, dashes, and punctuation).
- Each excerpt should be the shortest self-contained phrase that conveys the \
  information (typically 3-50 words). Include enough context so a reader can \
  understand the ground truth.
- If a category is NOT present in the protocol, map it to an empty list [].
- Do NOT paraphrase, summarize, condense, reorder, or relabel. Do NOT add \
  category labels (e.g. "Systemtherapie:", "Metastasierung:") that are not \
  literally in the source. Do NOT merge information from different lines or \
  fields into one excerpt — emit each contiguous source span as its own list \
  entry instead.
- Do NOT replace characters: keep the exact dash style (- vs ‐ vs –), the \
  exact spaces, and the exact line breaks from the source.
- Every excerpt must be findable via plain substring search in the original.
- Return ONLY valid JSON, no markdown fences, no explanation.
"""


def build_user_prompt(summary_text, items):
    items_block = "\n".join(
        f"  \"{slugify(item)}\": \"{item}\"" for item in items
    )
    return (
        f"## Summary text\n\n{summary_text}\n\n"
        f"## Categories (slug → label)\n\n{{{items_block}\n}}\n\n"
        f"Return the JSON mapping slug → list of exact excerpts."
    )


def call_openai(summary_text, items, model=None, system_prompt=None):
    from openai import OpenAI

    # Prefer an OpenAI-compatible endpoint configured via GROQ_* (same convention
    # as preprocess.py / generate_fragestellung.py); fall back to OPENAI_*.
    api_key = (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("GROQ_API_KEY")
    )
    base_url = (
        os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("GROQ_BASE_URL")
    )
    model = (
        model
        or os.environ.get("OPENAI_MODEL")
        or os.environ.get("GROQ_MODEL")
        or "gpt-4o"
    )
    if not api_key:
        raise RuntimeError(
            "No API key found. Set OPENAI_API_KEY or GROQ_API_KEY (in .env)."
        )

    client = OpenAI(api_key=api_key, base_url=base_url) if base_url \
        else OpenAI(api_key=api_key)
    user_msg = build_user_prompt(summary_text, items)
    # Allow override via env; 36 categories × ~4 excerpts can easily exceed
    # small defaults (Groq's default is 1024).
    max_tokens = int(os.environ.get("HIGHLIGHTS_MAX_TOKENS", "16384"))
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    raw = resp.choices[0].message.content
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Validation: check every returned excerpt actually exists in the text.
# LLMs frequently normalize Unicode (en-dash → hyphen, NBSP → space, smart
# quotes, collapsed whitespace, soft hyphens stripped). The app matches
# excerpts via plain str.find on the raw text, so we fuzzy-locate each
# excerpt in the raw text and rewrite it to the actual raw substring.
# ---------------------------------------------------------------------------
import unicodedata

# Characters that are visually equivalent but byte-different.
_DASH_CHARS = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"  # ‐‑‒–—―−
_QUOTE_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201A": "'", "\u201B": "'",
    "\u201C": '"', "\u201D": '"', "\u201E": '"', "\u201F": '"',
    "\u2032": "'", "\u2033": '"',
}


def _normalize_char(ch):
    """Map a single char to its canonical form. Returns '' to drop the char."""
    if ch in ("\u00AD",):  # soft hyphen
        return ""
    if ch in _DASH_CHARS:
        return "-"
    if ch in _QUOTE_MAP:
        return _QUOTE_MAP[ch]
    if ch in ("\u00A0", "\u202F", "\u2007"):  # NBSPs
        return " "
    if ch.isspace():
        return " "
    return ch


def _normalize_with_map(text):
    """Return (normalized_string, index_map) where index_map[i] is the index
    in the original text corresponding to position i in the normalized string.
    Adjacent whitespace is collapsed to a single space.
    """
    out_chars = []
    out_map = []
    prev_space = False
    for i, ch in enumerate(text):
        nc = _normalize_char(ch)
        if nc == "":
            continue
        if nc == " ":
            if prev_space:
                continue
            prev_space = True
        else:
            prev_space = False
        out_chars.append(nc)
        out_map.append(i)
    return "".join(out_chars), out_map


def _locate_excerpt(raw_text, excerpt):
    """Try to locate *excerpt* in *raw_text*.

    Returns the actual raw substring (so str.find will succeed) or None.
    """
    if not excerpt:
        return None
    # Fast path: literal match.
    if excerpt in raw_text:
        return excerpt

    # Normalize both sides (NFC + dash/quote/whitespace canonicalization).
    raw_nfc = unicodedata.normalize("NFC", raw_text)
    exc_nfc = unicodedata.normalize("NFC", excerpt).strip()
    norm_text, idx_map = _normalize_with_map(raw_nfc)
    norm_exc, _ = _normalize_with_map(exc_nfc)
    norm_exc = norm_exc.strip()
    if not norm_exc:
        return None

    pos = norm_text.find(norm_exc)
    if pos == -1:
        # Tier-2 fallback: alphanumeric-only match (ignores all punctuation
        # and whitespace differences such as LLM-inserted ';', removed
        # hyphens, paraphrased separators, etc.).
        return _locate_alphanumeric(raw_nfc, raw_text, exc_nfc)

    start = idx_map[pos]
    # End index in raw: char *after* the last matched normalized char.
    end_norm = pos + len(norm_exc) - 1
    if end_norm >= len(idx_map):
        return None
    end = idx_map[end_norm] + 1
    candidate = raw_nfc[start:end]
    # Sanity: the candidate must literally appear in the original raw_text.
    # (raw_nfc and raw_text differ only if NFC changed something; rare for our
    # German clinical text, but guard anyway.)
    if candidate in raw_text:
        return candidate
    # Fallback: try the unnormalized slice from raw_text directly.
    if raw_text[start:end] and raw_text[start:end] in raw_text:
        return raw_text[start:end]
    return _locate_alphanumeric(raw_nfc, raw_text, exc_nfc)


def _alnum_with_map(text):
    """Lowercased alphanumeric-only projection plus index map back into text."""
    out = []
    idx = []
    for i, ch in enumerate(text):
        if ch.isalnum():
            out.append(ch.lower())
            idx.append(i)
    return "".join(out), idx


def _locate_alphanumeric(raw_nfc, raw_text, excerpt_nfc):
    """Match using only alphanumeric content; return verbatim raw substring."""
    src_alnum, src_idx = _alnum_with_map(raw_nfc)
    exc_alnum, _ = _alnum_with_map(excerpt_nfc)
    if not exc_alnum:
        return None
    pos = src_alnum.find(exc_alnum)
    if pos == -1:
        return None
    end_alnum = pos + len(exc_alnum) - 1
    if end_alnum >= len(src_idx):
        return None
    start = src_idx[pos]
    end = src_idx[end_alnum] + 1
    candidate = raw_nfc[start:end]
    if candidate in raw_text:
        return candidate
    if raw_text[start:end] and raw_text[start:end] in raw_text:
        return raw_text[start:end]
    return None


def _split_and_locate(raw_text, excerpt, min_alnum=8):
    """Split *excerpt* on common separators and locate each piece in *raw_text*.

    Used as a tier-3 fallback when an excerpt is an LLM concatenation of
    several source fields (e.g. "Beginn Syst. Th.: 19.12.2022 Ende Syst. Th.:
    21.02.2023 Protokoll: ..."). Returns a list of verbatim raw substrings
    (de-duplicated, in source order). Pieces with fewer than *min_alnum*
    alphanumeric chars are ignored to avoid spurious one-word matches.
    """
    # Split on: newline, semicolon, bullet, ' - ', ' – ', ' / ', ' | '.
    # Keep commas/periods inside pieces — they are common inside dates/values.
    parts = re.split(r"[\n;•·]| - | – | \| ", excerpt)
    located = []
    seen = set()
    for part in parts:
        part = part.strip(" \t,.;:•·-–—")
        if sum(ch.isalnum() for ch in part) < min_alnum:
            continue
        hit = _locate_excerpt(raw_text, part)
        if hit and hit not in seen:
            seen.add(hit)
            located.append(hit)
    # Order results by position in raw_text so the highlight regions stay
    # readable when rendered.
    located.sort(key=lambda s: raw_text.find(s))
    return located


def repair_mapping(mapping, raw_text):
    """Rewrite excerpts to their verbatim raw-text form when fuzzy-locatable.

    Mutates *mapping* in place and returns a list of (slug, original_excerpt)
    for excerpts that could not be located even after splitting.
    """
    unresolved = []
    for slug, excerpts in list(mapping.items()):
        new_list = []
        seen = set()
        for exc in excerpts:
            located = _locate_excerpt(raw_text, exc)
            if located is not None:
                if located not in seen:
                    seen.add(located)
                    new_list.append(located)
                continue
            # Tier-3: split-and-locate for LLM-concatenated excerpts.
            pieces = _split_and_locate(raw_text, exc)
            if pieces:
                for p in pieces:
                    if p not in seen:
                        seen.add(p)
                        new_list.append(p)
                continue
            unresolved.append((slug, exc))
        mapping[slug] = new_list
    return unresolved


def validate_mapping(mapping, text):
    """Return list of (slug, excerpt) entries that are NOT verbatim in text."""
    issues = []
    for slug, excerpts in mapping.items():
        for exc in excerpts:
            if exc not in text:
                issues.append((slug, exc))
    return issues


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Generate highlight mappings.")
    parser.add_argument("--case", type=str,
                        help="Process only this case_id (file stem / hash). "
                             "Forces re-run of all sources for that case.")
    parser.add_argument("--model", default=None,
                        help="Model id. Defaults to OPENAI_MODEL/GROQ_MODEL "
                             "from the environment, else 'gpt-4o'.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print prompts without calling LLM"
    )
    args = parser.parse_args()

    cases = discover_cases()
    if args.case:
        cases = [c for c in cases if c[0] == args.case]
        if not cases:
            print(f"case_id {args.case!r} not found in {DOCUMENTS_DIR}.")
            sys.exit(1)

    # Load existing mappings to allow incremental updates
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            all_mappings = json.load(f)
    else:
        all_mappings = {}

    for case_id, human_text, llm_text, protocol_text in cases:
        if case_id not in all_mappings:
            all_mappings[case_id] = {}

        sources = [
            ("human_summary", human_text, SYSTEM_PROMPT),
            ("llm_summary", llm_text, SYSTEM_PROMPT),
            ("protocol", protocol_text, PROTOCOL_SYSTEM_PROMPT),
        ]

        for version_key, text, sys_prompt in sources:
            if not text:
                print(f"  {case_id} / {version_key}: no text, skipping")
                continue

            # Skip if already present (incremental mode); --case forces rerun
            if version_key in all_mappings[case_id] and not args.case:
                print(f"  {case_id} / {version_key}: already exists, skipping")
                continue

            print(f"Processing {case_id} / {version_key} ...")

            if args.dry_run:
                print(build_user_prompt(text, INFO_ITEMS))
                print("---")
                continue

            mapping = call_openai(text, INFO_ITEMS, model=args.model,
                                  system_prompt=sys_prompt)

            # Repair excerpts: rewrite to verbatim raw substrings via fuzzy
            # (Unicode/whitespace-tolerant) location. Anything still unresolved
            # is dropped so the app's str.find-based highlighter never misses.
            unresolved = repair_mapping(mapping, text)
            if unresolved:
                print(f"  ⚠ {len(unresolved)} excerpt(s) could not be located "
                      f"(dropped):")
                for slug, exc in unresolved[:5]:
                    snippet = exc.replace("\n", " ")[:80]
                    print(f"    {slug}: \"{snippet}...\"")

            # Final sanity check
            remaining = validate_mapping(mapping, text)
            if remaining:
                print(f"  ✗ {len(remaining)} excerpt(s) STILL not verbatim "
                      f"after repair (dropping):")
                for slug, exc in remaining:
                    mapping[slug] = [e for e in mapping[slug] if e != exc]

            all_mappings[case_id][version_key] = mapping
            # Be polite to the API
            time.sleep(1)

    if not args.dry_run:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(all_mappings, f, indent=2, ensure_ascii=False)
        print(f"\nSaved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
