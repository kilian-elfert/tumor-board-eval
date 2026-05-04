#!/usr/bin/env python3
"""Filter Tumorboard Fragestellungen by actionability.

For every protocol .txt in the source directory (default: C:/Users/kilia/Desktop/main_data),
this script:

1. Extracts the Fragestellung block (between the SECOND occurrence of
   "Fragestellung" and the next "Beschluss") -- same logic as
   Bearbeitung/main.py::_extract_fragestellung_section.
2. Decides whether the Fragestellung puts forward at least one concrete,
   guideline-checkable plan / therapy option / diagnostic next step
   (INCLUDE) or is just a generic procedural placeholder such as
   "Weiteres Procedere?" / "Bitte um Festlegung des weiteren Procederes"
   (EXCLUDE).
3. Uses a hybrid pipeline:
     - cheap deterministic rule auto-excludes obvious stubs;
     - everything else is sent to Groq (gpt-oss-120b) for a structured
       JSON judgment with reason and the actionable items it found.
4. Writes results to <out>/decisions.jsonl, <out>/summary.csv,
   and copies of the kept / dropped Fragestellungen into
   <out>/included/ and <out>/excluded/.

Usage:
    uv run filter_fragestellungen.py
    uv run filter_fragestellungen.py --limit 10
    uv run filter_fragestellungen.py --case <hash> --overwrite
    uv run filter_fragestellungen.py --no-llm          # rule only
    uv run filter_fragestellungen.py --workers 4
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
import httpx


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

load_dotenv(override=True)

DEFAULT_SOURCE = Path(r"C:\Users\kilia\Desktop\main_data")
DEFAULT_OUTPUT = Path(r"C:\Users\kilia\Desktop\main_data_filtered")

GROQ_API_KEY  = os.environ.get("GROQ_API_KEY", "")
GROQ_BASE_URL = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MODEL    = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

HASH_RE = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Fragestellung extraction (ported from Bearbeitung/main.py)
# ---------------------------------------------------------------------------

def read_text(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=enc).lstrip("\ufeff")
        except UnicodeDecodeError:
            continue
    return ""


_FRAGE_RE = re.compile(
    r"(?im)^[ \t]*(?:Fragestellung|Frage)[ \t]*:",
)
_BESCHLUSS_RE = re.compile(
    r"(?im)^[ \t]*Beschluss[ \t]*:",
)


def extract_fragestellung(text: str) -> str:
    """Return the content of the Fragestellung section.

    - Recognises both 'Fragestellung:' and 'Frage:' as section headers
      (case-insensitive, must appear at the start of a line followed by ':'
      so that a stray mention inside a sentence is not mistaken for a header).
    - If multiple such headers exist, the LAST one is used (earlier ones are
      usually metadata or table-of-contents entries).
    - The section ends at the next 'Beschluss:' header, or end of document.
    Whitespace is collapsed into a single clean paragraph.
    """
    matches = list(_FRAGE_RE.finditer(text))
    if not matches:
        return ""
    last = matches[-1]
    after = last.end()
    bm = _BESCHLUSS_RE.search(text, after)
    raw = text[after:bm.start()] if bm else text[after:]
    return " ".join(raw.split())


# ---------------------------------------------------------------------------
# deterministic pre-filter
# ---------------------------------------------------------------------------

# Tokens that count as "purely procedural boilerplate" and carry no clinical
# proposition on their own. Used only to decide if a SHORT Fragestellung is
# nothing but boilerplate; long Fragestellungen always go to the LLM even if
# they happen to end with one of these phrases.
_BOILERPLATE_TOKENS = {
    "weiteres", "weitere", "weiter",
    "bitte", "um", "wir", "bitten",
    "festlegung", "festlegen", "festzulegen",
    "demonstration", "demo", "demonstrieren",
    "vorstellung", "vorstellen",
    "befund", "befunde", "befundes", "befundung",
    "fall", "falles", "fallvorstellung",
    "tumorboard", "tumorkonferenz", "konferenz", "tk", "htk", "hkz",
    "der", "den", "des", "die", "das", "dem", "ein", "eines", "einer",
    "und", "oder", "sowie", "bzw", "ggf",
    "procedere", "prozedere", "proc", "proz", "procederes", "prozederes",
    "prozedeere",
    "frage", "fragen",
    "nach", "fuer", "fur",
    "diskussion", "besprechung",
}

_PUNCT_RE = re.compile(r"[\.\,\:\;\!\?\(\)\[\]\{\}\"'\u00ab\u00bb\u2013\u2014\-/]+")


def _tokens(text: str) -> list[str]:
    cleaned = _PUNCT_RE.sub(" ", text.lower())
    return [t for t in cleaned.split() if t]


def rule_decision(fragestellung: str) -> dict | None:
    """Return a decision dict if the rule is confident, else None.

    Confident EXCLUDE: every alphabetic token is in the boilerplate vocabulary
    AND total token count is small (<= 12). This catches things like
    'Weiteres Procedere?' or 'Bitte um Festlegung des weiteren Procederes'
    while NEVER misclassifying a substantive Fragestellung that just happens
    to end with such a phrase.
    """
    text = fragestellung.strip()
    if not text:
        return {
            "include": False,
            "reason": "Empty Fragestellung (no text between 'Fragestellung' "
                      "and 'Beschluss').",
            "proposed_actions": [],
            "source": "rule:empty",
            "confidence": 1.0,
        }

    toks = _tokens(text)
    if not toks:
        return {
            "include": False,
            "reason": "Fragestellung contains no alphabetic tokens.",
            "proposed_actions": [],
            "source": "rule:empty",
            "confidence": 1.0,
        }

    if len(toks) <= 12 and all(t in _BOILERPLATE_TOKENS for t in toks):
        return {
            "include": False,
            "reason": ("Fragestellung is pure procedural boilerplate "
                       f"({len(toks)} tokens, no clinical content): "
                       f"{text!r}"),
            "proposed_actions": [],
            "source": "rule:boilerplate-only",
            "confidence": 1.0,
        }

    return None  # ambiguous -> escalate to LLM


# ---------------------------------------------------------------------------
# LLM judge
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Du bist ein erfahrener dermato-onkologischer Gutachter.
Deine Aufgabe ist es, fuer eine Tumorboard-Fragestellung zu entscheiden, ob
sie mindestens einen KONKRETEN, FALSIFIZIERBAREN Vorschlag enthaelt, der
gegen die S3-Leitlinie Melanom geprueft werden koennte.

EINSCHLUSS (include = true), wenn die Fragestellung MINDESTENS EINES davon
nennt:
- eine konkrete Therapieoption (Wirkstoff, Schema, Klasse wie ICI/BRAF-MEK,
  Radiotherapie, OP/Resektion, lokale Verfahren, Studienteilnahme),
- einen konkreten diagnostischen Schritt (z.B. spezifische Bildgebung,
  Mutationsanalyse, Biopsie, LK-Sonographie, SLNE),
- ein konkretes Vorgehen (Therapiepause, Watch-and-Wait, Nachsorgeintervall,
  Wechsel/Eskalation/Deeskalation einer Therapie),
- oder eine konkrete falsifizierbare Frage zu einem solchen Vorgehen.

AUSSCHLUSS (include = false), wenn die Fragestellung nur generische
Floskeln enthaelt wie:
- "Weiteres Procedere?"
- "Bitte um Festlegung des weiteren Procederes"
- "Bitte um Demonstration und Procedere"
- "Vorstellung und Procedere"
und KEINE konkrete Option oder Plan benennt.

WICHTIG:
- Eine Fragestellung, die einen klinischen Verlauf, Befunde oder Optionen
  beschreibt UND zusaetzlich eine Procedere-Floskel enthaelt, gilt als
  EINGESCHLOSSEN, sofern mindestens eine konkrete Option/Plan ableitbar ist.
- Wertfreie Patientenbeschreibung allein (Alter, Stadium, Verlauf) ohne
  jegliche Handlungsoption => AUSSCHLUSS.

Antworte AUSSCHLIESSLICH mit einem gueltigen JSON-Objekt mit folgendem
Schema (keine Markdown-Codeblock-Auszeichnung, kein Kommentar):

{
  "include": <true|false>,
  "confidence": <Zahl zwischen 0 und 1>,
  "reason": "<kurze deutsche Begruendung, max. 2 Saetze>",
  "proposed_actions": ["<konkrete Aktion 1>", "<konkrete Aktion 2>", ...]
}

Wenn include=false ist, MUSS proposed_actions eine leere Liste sein."""


USER_PROMPT_TEMPLATE = """Hier die zu beurteilende Fragestellung:

\"\"\"
{fragestellung}
\"\"\"

Entscheide gemaess der oben definierten Kriterien."""


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


def llm_judge(fragestellung: str, *, timeout: float = 120.0,
              max_retries: int = 2) -> dict:
    if not GROQ_API_KEY:
        raise SystemExit("ERROR: GROQ_API_KEY not set in environment "
                         "(.env or shell). Use --no-llm for rule-only mode.")

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": USER_PROMPT_TEMPLATE.format(fragestellung=fragestellung)},
        ],
        "temperature": 0,
        "max_completion_tokens": 1024,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            r = httpx.post(
                f"{GROQ_BASE_URL}/chat/completions",
                headers=headers,
                content=body,
                timeout=timeout,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            data = json.loads(_strip_code_fence(content))
            return {
                "include": bool(data.get("include")),
                "confidence": float(data.get("confidence", 0.5)),
                "reason": str(data.get("reason", "")).strip(),
                "proposed_actions": [
                    str(a).strip() for a in data.get("proposed_actions", [])
                    if str(a).strip()
                ],
                "source": "llm",
            }
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"LLM call failed after {max_retries + 1} attempts: "
                       f"{last_err}")


# ---------------------------------------------------------------------------
# discovery / orchestration
# ---------------------------------------------------------------------------

def discover_protocol_files(source_dir: Path) -> list[Path]:
    """Return the list of <hash>.txt protocol files at the root of source_dir.

    Excludes the per-case auxiliary files (_lab.txt, _verlaufsdoku*, ...).
    """
    result = []
    for entry in sorted(source_dir.iterdir()):
        if not entry.is_file() or entry.suffix.lower() != ".txt":
            continue
        stem = entry.stem
        if HASH_RE.match(stem):
            result.append(entry)
    return result


def slugify_for_csv(text: str, n: int = 120) -> str:
    snippet = " ".join(text.split())
    if len(snippet) <= n:
        return snippet
    return snippet[: n - 1] + "\u2026"


def process_case(protocol_path: Path, *, use_llm: bool) -> dict:
    case_id = protocol_path.stem
    raw = read_text(protocol_path)
    fragestellung = extract_fragestellung(raw)

    rule = rule_decision(fragestellung)
    if rule is not None:
        decision = rule
    elif use_llm:
        try:
            decision = llm_judge(fragestellung)
        except Exception as e:
            decision = {
                "include": False,
                "confidence": 0.0,
                "reason": f"LLM error: {e}",
                "proposed_actions": [],
                "source": "llm:error",
            }
    else:
        # Without LLM and no rule match -> default to INCLUDE (conservative:
        # keep cases for manual review rather than silently dropping them).
        decision = {
            "include": True,
            "confidence": 0.5,
            "reason": "No rule match and --no-llm: kept for manual review.",
            "proposed_actions": [],
            "source": "rule:default-include",
        }

    return {
        "case_id": case_id,
        "fragestellung": fragestellung,
        **decision,
    }


# ---------------------------------------------------------------------------
# output writers
# ---------------------------------------------------------------------------

def write_outputs(records: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    included_dir = out_dir / "included"
    excluded_dir = out_dir / "excluded"
    included_dir.mkdir(exist_ok=True)
    excluded_dir.mkdir(exist_ok=True)

    # decisions.jsonl
    with (out_dir / "decisions.jsonl").open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # summary.csv
    with (out_dir / "summary.csv").open("w", encoding="utf-8",
                                        newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow([
            "case_id", "decision", "confidence", "source",
            "n_actions", "actions", "reason", "fragestellung_snippet",
        ])
        for rec in records:
            w.writerow([
                rec["case_id"],
                "INCLUDE" if rec["include"] else "EXCLUDE",
                f'{rec["confidence"]:.2f}',
                rec["source"],
                len(rec["proposed_actions"]),
                " | ".join(rec["proposed_actions"]),
                rec["reason"],
                slugify_for_csv(rec["fragestellung"]),
            ])

    # per-case copies for manual audit
    for rec in records:
        target = (included_dir if rec["include"] else excluded_dir) \
                 / f'{rec["case_id"]}.txt'
        target.write_text(rec["fragestellung"], encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                    help=f"Folder with <hash>.txt protocols "
                         f"(default: {DEFAULT_SOURCE}).")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUTPUT,
                    help=f"Output folder (default: {DEFAULT_OUTPUT}).")
    ap.add_argument("--case", action="append", default=[],
                    help="Process only this case_id (may be repeated).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process only the first N cases (debug).")
    ap.add_argument("--no-llm", action="store_true",
                    help="Skip LLM; use only the deterministic rule.")
    ap.add_argument("--workers", type=int, default=4,
                    help="Parallel LLM workers (default: 4).")
    ap.add_argument("--overwrite", action="store_true",
                    help="Overwrite existing outputs.")
    return ap.parse_args()


def main() -> int:
    args = parse_args()

    if not args.source.is_dir():
        print(f"ERROR: --source not a directory: {args.source}",
              file=sys.stderr)
        return 2

    files = discover_protocol_files(args.source)
    if args.case:
        wanted = set(args.case)
        files = [f for f in files if f.stem in wanted]
    if args.limit is not None:
        files = files[: args.limit]

    if not files:
        print("No protocol files matched.", file=sys.stderr)
        return 1

    if args.out.exists() and not args.overwrite \
       and (args.out / "decisions.jsonl").exists():
        print(f"ERROR: outputs already exist in {args.out}. "
              f"Use --overwrite.", file=sys.stderr)
        return 2

    print(f"Processing {len(files)} cases from {args.source}")
    print(f"  LLM: {'OFF (rule only)' if args.no_llm else GROQ_MODEL}")
    print(f"  Output: {args.out}\n")

    results: list[dict] = []
    lock = threading.Lock()
    done = 0

    def _runner(p: Path) -> dict:
        return process_case(p, use_llm=not args.no_llm)

    workers = 1 if args.no_llm else max(1, args.workers)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_runner, p): p for p in files}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                rec = fut.result()
            except Exception as e:
                rec = {
                    "case_id": p.stem,
                    "fragestellung": "",
                    "include": False,
                    "confidence": 0.0,
                    "reason": f"FATAL: {e}",
                    "proposed_actions": [],
                    "source": "error",
                }
            with lock:
                results.append(rec)
                done += 1
                tag = "OK " if rec["include"] else "OUT"
                print(f"  [{done:>3}/{len(files)}] {tag} {rec['case_id'][:12]}"
                      f"\u2026  ({rec['source']})  {rec['reason'][:80]}")

    results.sort(key=lambda r: r["case_id"])
    write_outputs(results, args.out)

    n_in = sum(1 for r in results if r["include"])
    n_out = len(results) - n_in
    print(f"\nDone. Included: {n_in}   Excluded: {n_out}")
    print(f"Outputs: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
