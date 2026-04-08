#!/usr/bin/env python3
"""
Pre-generate highlight mappings for the Falschinformationen step.

For each case and each summary version (human / llm), this script asks an LLM
to identify the exact text excerpts that correspond to each INFO_ITEM.
The result is saved to  highlight_mappings.json  which the app loads at runtime.

Usage:
    # Set your API key first
    set OPENAI_API_KEY=sk-...

    python generate_highlights.py            # process all cases
    python generate_highlights.py --case 1   # process only Fall 1
    python generate_highlights.py --dry-run  # print prompts, don't call LLM
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
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEXTS_HUMAN_DIR = os.path.join(BASE_DIR, "texts_human")
TEXTS_LLM_DIR = os.path.join(BASE_DIR, "texts_llm")
DOCUMENTS_DIR = os.path.join(BASE_DIR, "original_documents")
OUTPUT_FILE = os.path.join(BASE_DIR, "highlight_mappings.json")

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
# Discover text files (same logic as app.py)
# ---------------------------------------------------------------------------
def _find_text_file(directory, pattern_prefix):
    if not os.path.isdir(directory):
        return None
    prefix_lower = pattern_prefix.lower()
    for fname in os.listdir(directory):
        if fname.lower().startswith(prefix_lower) and fname.lower().endswith(".txt"):
            return os.path.join(directory, fname)
    return None


def _read_text_file(path):
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def discover_cases():
    """Return list of (fall_nr, human_summary, llm_summary, protocol_text)."""
    cases = []
    for fname in os.listdir(DOCUMENTS_DIR):
        if not fname.lower().endswith(".pdf"):
            continue
        m = re.match(r"[Ff]all(\d+)", fname)
        if m:
            fall_nr = int(m.group(1))
            prefix = f"fall_{fall_nr}"
            human = _read_text_file(
                _find_text_file(
                    os.path.join(TEXTS_HUMAN_DIR, "zusammenfassung"), prefix
                )
            )
            llm = _read_text_file(
                _find_text_file(
                    os.path.join(TEXTS_LLM_DIR, "zusammenfassungen"), prefix
                )
            )
            # Protocol text: look for .txt next to PDF with same stem
            txt_path = os.path.join(DOCUMENTS_DIR, os.path.splitext(fname)[0] + ".txt")
            protocol = _read_text_file(txt_path)
            cases.append((fall_nr, human, llm, protocol))
    cases.sort()
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
  excerpts from the summary text (copy-paste precision).
- Each excerpt should be the shortest self-contained phrase that conveys the \
  information (typically 3-30 words). Avoid single words unless that is all \
  that is present.
- If a category is NOT present in the text, map it to an empty list [].
- Do NOT paraphrase or modify the text. The excerpts must be findable verbatim \
  in the original.
- Return ONLY valid JSON, no markdown fences, no explanation.
"""

PROTOCOL_SYSTEM_PROMPT = """\
You are a medical text analysis assistant. Given an original tumor board \
protocol and a list of information categories, extract the EXACT text excerpts \
from the protocol that correspond to each category.

Rules:
- Return a JSON object mapping each category slug to a list of exact verbatim \
  excerpts from the protocol (copy-paste precision).
- Each excerpt should be the shortest self-contained phrase that conveys the \
  information (typically 3-50 words). Include enough context so a reader can \
  understand the ground truth.
- If a category is NOT present in the protocol, map it to an empty list [].
- Do NOT paraphrase or modify the text. The excerpts must be findable verbatim \
  in the original.
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


def call_openai(summary_text, items, model="gpt-4o", system_prompt=None):
    from openai import OpenAI

    client = OpenAI()  # uses OPENAI_API_KEY env var
    user_msg = build_user_prompt(summary_text, items)
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
    )
    raw = resp.choices[0].message.content
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Validation: check every returned excerpt actually exists in the text
# ---------------------------------------------------------------------------
def validate_mapping(mapping, text):
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
    parser.add_argument("--case", type=int, help="Process only this Fall number")
    parser.add_argument("--model", default="gpt-4o", help="OpenAI model to use")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print prompts without calling LLM"
    )
    args = parser.parse_args()

    cases = discover_cases()
    if args.case:
        cases = [(n, h, l) for n, h, l in cases if n == args.case]
        if not cases:
            print(f"Fall {args.case} not found.")
            sys.exit(1)

    # Load existing mappings to allow incremental updates
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            all_mappings = json.load(f)
    else:
        all_mappings = {}

    for fall_nr, human_text, llm_text, protocol_text in cases:
        case_key = str(fall_nr)
        if case_key not in all_mappings:
            all_mappings[case_key] = {}

        sources = [
            ("human_summary", human_text, SYSTEM_PROMPT),
            ("llm_summary", llm_text, SYSTEM_PROMPT),
            ("protocol", protocol_text, PROTOCOL_SYSTEM_PROMPT),
        ]

        for version_key, text, sys_prompt in sources:
            if not text:
                print(f"  Fall {fall_nr} / {version_key}: no text, skipping")
                continue

            # Skip if already present (incremental mode)
            if version_key in all_mappings[case_key] and not args.case:
                print(f"  Fall {fall_nr} / {version_key}: already exists, skipping")
                continue

            print(f"Processing Fall {fall_nr} / {version_key} ...")

            if args.dry_run:
                print(build_user_prompt(text, INFO_ITEMS))
                print("---")
                continue

            mapping = call_openai(text, INFO_ITEMS, model=args.model,
                                  system_prompt=sys_prompt)

            # Validate
            issues = validate_mapping(mapping, text)
            if issues:
                print(f"  ⚠ {len(issues)} excerpt(s) not found verbatim:")
                for slug, exc in issues[:5]:
                    print(f"    {slug}: \"{exc[:80]}...\"")
                # Remove bad excerpts
                for slug, exc in issues:
                    if slug in mapping:
                        mapping[slug] = [e for e in mapping[slug] if e != exc]

            all_mappings[case_key][version_key] = mapping
            # Be polite to the API
            time.sleep(1)

    if not args.dry_run:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(all_mappings, f, indent=2, ensure_ascii=False)
        print(f"\nSaved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
