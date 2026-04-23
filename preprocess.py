#!/usr/bin/env python3
"""Pre-process raw case data into dashboard JSON files.

Reads protocol text, lab values, timeline JSONL, and imaging directories
for each case and produces a {case_id}_dashboard.json that the Flask app
loads directly into PATIENT_DATA.

Deterministic fields (demographics, lab, timeline, imaging metadata) are
parsed without an LLM.  Clinical fields (body map, staging, mutations,
therapies, comorbidities, etc.) are extracted via an OpenAI LLM call.

Usage:
    uv run preprocess.py                  # all cases from CSV
    uv run preprocess.py --case HASH      # single case by hash ID
    uv run preprocess.py --legacy         # re-process legacy Fall1..5
    uv run preprocess.py --dry-run        # preview without writing files
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime

from dotenv import load_dotenv
import httpx

load_dotenv(override=True)

# ── paths ────────────────────────────────────────────────────────────────────

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_DIR   = os.path.join(BASE_DIR, 'dashboard_data')
DOCUMENTS_DIR   = os.path.join(BASE_DIR, 'original_documents')

DATA_ROOT       = os.path.expanduser(os.environ.get('DATA_ROOT', ''))
SOURCES_DIR     = os.path.join(DATA_ROOT, os.environ.get('SOURCES_SUBDIR', 'sources')) if DATA_ROOT else ''
OUTPUTS_DIR_EXT = os.path.join(DATA_ROOT, os.environ.get('OUTPUTS_SUBDIR', 'outputs')) if DATA_ROOT else ''
CSV_FILENAME    = os.environ.get('CSV_FILENAME', 'sampled_cases.csv')
CSV_PATH        = os.path.join(DATA_ROOT, CSV_FILENAME) if DATA_ROOT else ''

LLM_API_KEY  = os.environ.get('LLM_API_KEY', '')
LLM_BASE_URL = os.environ.get('LLM_BASE_URL', 'http://10.99.0.230:8004/v1')
LLM_MODEL    = os.environ.get('LLM_MODEL', 'openai/gpt-oss-120b')

# ── valid body region IDs (for LLM prompt) ───────────────────────────────────

BODY_REGION_IDS = [
    "head", "head_mouth", "head_neck", "neck",
    "chest_right", "chest_left", "thorax", "thorax_abdomen",
    "lung", "breast_right", "abdomen", "abdomen_left", "abdomen_subcutaneous",
    "spine", "back_center", "back_left_scapula",
    "pelvis", "groin_right", "groin_left",
    "thigh_left", "thigh_right",
    "axilla_left", "axilla_right",
    "left_forearm", "right_forearm",
    "whole_body",
]

# ── LLM helper (raw httpx – OpenAI-compatible /chat/completions) ────────────

def _llm_chat(messages, max_tokens=8192, json_mode=False):
    """Call the configured OpenAI-compatible chat endpoint and return the content string."""
    payload = {
        'model': LLM_MODEL,
        'max_completion_tokens': max_tokens,
        'messages': messages,
    }
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}
    headers = {'Content-Type': 'application/json'}
    if LLM_API_KEY:
        headers['Authorization'] = f'Bearer {LLM_API_KEY}'
    r = httpx.post(
        f'{LLM_BASE_URL}/chat/completions',
        headers=headers,
        content=json.dumps(payload, ensure_ascii=False),
        timeout=120,
    )
    r.raise_for_status()
    return r.json()['choices'][0]['message']['content']

# ── deterministic parsers ────────────────────────────────────────────────────

def _read_text_file(path):
    if path and os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    return ''


def _format_date(date_str):
    if not date_str:
        return ''
    for fmt in ('%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            clean = re.sub(r'[+-]\d{2}:\d{2}$', '', date_str).rstrip('Z')
            dt = datetime.strptime(clean, fmt)
            return dt.strftime('%d.%m.%Y')
        except ValueError:
            continue
    if re.match(r'\d{2}\.\d{2}\.\d{4}', date_str):
        return date_str
    return date_str


def _calculate_age(birth_date_str, reference_date_str=None):
    if not birth_date_str:
        return ''
    try:
        bd = datetime.strptime(birth_date_str, '%Y-%m-%d')
        if reference_date_str:
            ref_str = reference_date_str.strip().split(' ')[0]
            ref = datetime.strptime(ref_str, '%Y-%m-%d')
        else:
            ref = datetime.now()
        age = ref.year - bd.year - ((ref.month, ref.day) < (bd.month, bd.day))
        return age
    except ValueError:
        return ''


def parse_verlaufsdoku(case_id, sources_dir):
    """Parse JSONL timeline file.  Returns list of {date, event, type}."""
    jsonl_path = os.path.join(sources_dir, f'{case_id}_verlaufsdoku.jsonl')
    entries = []
    if not os.path.isfile(jsonl_path):
        return entries
    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                raw_date = entry.get('date', '')
                entries.append({
                    'type':  entry.get('type', 'other'),
                    'date':  _format_date(raw_date),
                    'event': entry.get('text', entry.get('title', '')),
                    '_raw_date': raw_date,
                })
            except json.JSONDecodeError:
                continue
    entries.sort(key=lambda e: e.get('_raw_date', ''))
    for e in entries:
        del e['_raw_date']
    return entries


_LAB_LINE_RE = re.compile(
    r'^(?P<marker>[^:]+):\s*'
    r'(?P<value>\S+)'
    r'(?:\s+(?P<unit>[^\(D]+?))?'
    r'(?:\s*\(ref\s+(?P<ref>[^)]+)\))?'
    r'\s*Date:\s*(?P<date>\S+)',
)


def parse_lab_values(case_id, sources_dir):
    """Parse lab text file.  Returns list of {date, marker, value, unit, ref}."""
    lab_path = os.path.join(sources_dir, f'{case_id}_lab.txt')
    if not os.path.isfile(lab_path):
        return []
    content = _read_text_file(lab_path)
    if not content:
        return []
    results = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('['):
            continue
        m = _LAB_LINE_RE.match(line)
        if m:
            results.append({
                'date':   _format_date(m.group('date')),
                'marker': m.group('marker').strip(),
                'value':  m.group('value').strip(),
                'unit':   (m.group('unit') or '').strip(),
                'ref':    (m.group('ref') or '').strip(),
            })
    return results


def parse_imaging(case_id, sources_dir):
    """Scan imaging directories.  Returns list of imaging dicts."""
    imaging = []
    type_map = {'_ct': 'CT', '_pet_ct': 'PET-CT', '_mrt': 'MRT', '_sono': 'Sonographie'}
    for suffix, img_type in type_map.items():
        dir_path = os.path.join(sources_dir, f'{case_id}{suffix}')
        if not os.path.isdir(dir_path):
            continue
        files = sorted(os.listdir(dir_path))
        bases = {}
        for fname in files:
            if fname.endswith('.txt') or fname.endswith('.pdf'):
                base = fname.rsplit('.', 1)[0]
                bases.setdefault(base, {})[fname.rsplit('.', 1)[1]] = fname
        for base, exts in sorted(bases.items()):
            parts = base.rsplit('_', 1)
            date_str = _format_date(parts[-1]) if len(parts) >= 2 else ''
            txt_content = ''
            if 'txt' in exts:
                txt_content = _read_text_file(os.path.join(dir_path, exts['txt']))
            pdf_filename = exts.get('pdf', '')
            imaging.append({
                'date':         date_str,
                'type':         img_type,
                'modality':     img_type,
                'finding':      txt_content or '',
                'assessment':   '',
                'has_pdf':      bool(pdf_filename),
                'pdf_filename': pdf_filename,
                'img_dir':      f'{case_id}{suffix}',
            })
    return imaging


def read_protocol_text(case_id, sources_dir):
    """Read the tumor board protocol text for a case."""
    # External mode
    txt_path = os.path.join(sources_dir, f'{case_id}.txt')
    if os.path.isfile(txt_path):
        return _read_text_file(txt_path)
    # Legacy mode: scan original_documents/
    if os.path.isdir(DOCUMENTS_DIR):
        for fname in os.listdir(DOCUMENTS_DIR):
            if fname.lower().endswith('.txt'):
                m = re.match(r'[Ff]all(\d+)', fname)
                if m and m.group(1) == str(case_id):
                    return _read_text_file(os.path.join(DOCUMENTS_DIR, fname))
    return ''


# ── LLM extraction ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Du bist ein medizinischer Datenextraktor. Du erhältst ein Tumorkonferenz-Protokoll
eines Melanom-Falls und extrahierst daraus strukturierte Daten als JSON.

Die Ausgabe MUSS exakt folgendem JSON-Schema entsprechen. Lasse kein Feld aus.
Verwende leere Strings "" für fehlende Textfelder, leere Listen [] für fehlende Arrays,
und leere Objekte {} für fehlende Objekte.

{
  "name": "Nachname, Vorname",
  "dob": "DD.MM.YYYY",
  "age": <int oder ""> ,
  "sex": "M" oder "W",
  "location": "PLZ Ort",
  "pat_id": "PatientenID",

  "comorbidities": ["Komorbidität 1", ...],
  "ecog": "0 – Beschreibung" oder "",
  "karnofsky": "100 % – Beschreibung" oder "",

  "primary_diagnosis": "Diagnose-Text",
  "primary_location": "Anatomische Lokalisation",
  "diagnosis_date": "DD.MM.YYYY",
  "tumor_thickness": "X mm",
  "ulceration": "Ja" / "Nein" / "Nicht angegeben",
  "mitoses": "Wert oder ''",
  "histology": "Histologie-Text",
  "resection_status": "R0/R1/Rx/...",
  "safety_margin": "X cm",
  "slne": "Ergebnis-Text",

  "initial_staging": {
    "t": "pT...", "n": "pN...", "m": "cM...",
    "uicc": "Stadium", "classification": "AJCC 2017"
  },
  "staging": {
    "t": "pT...", "n": "pN...", "m": "cM...",
    "uicc": "Stadium", "classification": "AJCC 2017",
    "date": "DD.MM.YYYY"
  },
  WICHTIG zu staging: Extrahiere das 'Aktuelles Stadium' GENAU so, wie es im Text steht.
  ERFINDE KEINE Werte. Wenn im Text 'pT2 cN cM1a' steht, dann muss staging.t='pT2', staging.n='cN', staging.m='cM1a' sein.
  Leite KEINE eigenen Staging-Werte aus der Krankengeschichte ab. Nur wörtlich übernehmen.

  "mutations": {"BRAF": "V600E", "NRAS": "Wildtyp", ...},
  "pdl1": "Wert oder 'Nicht bestimmt'",
  "hla_a": "Ergebnis (z.B. 'HLA-A*02:01 positiv', 'negativ', 'noch nicht erfolgt') oder 'Nicht bestimmt'",

  "primary_tumor_body": ["region_id"],
  "primary_tumor_status": "active" oder "inactive",
  "primary_inactive_reason": "Begründung oder ''",

  "metastases_body": ["region_id", ...],
  "metastases_detail": [
    {
      "region": "region_id",
      "date": "DD.MM.YYYY",
      "label": "Beschreibung der Metastase",
      "status": "active" / "inactive" / "responding",
      "inactive_reason": "Begründung (nur bei inactive)"
    }
  ],

  "therapies": {
    "surgery": [{"date": "DD.MM.YYYY", "text": "Beschreibung"}],
    "radiation": [{"date": "DD.MM.YYYY", "text": "Beschreibung"}],
    "systemic": [{"date": "DD.MM.YYYY", "text": "Beschreibung"}],
    "current": [{"date": "seit MM/YYYY", "text": "Beschreibung"}]
  },

  "disease_status": "Freitext Status"
}

WICHTIG für body map Regionen — verwende NUR diese IDs:
""" + json.dumps(BODY_REGION_IDS) + """

Regeln für die Zuordnung:
- "primary_tumor_body": Mappe die Primärtumor-Lokalisation auf die passende(n) Region-ID(s).
  Beispiele: "Unterarm rechts" → ["right_forearm"], "Kopf" → ["head"],
  "Abdomen links" → ["abdomen_left"], "Rücken" → ["back_center"],
  "Oberschenkel links" → ["thigh_left"], "Oberschenkel rechts" → ["thigh_right"]
- "metastases_detail": Jede Metastase bekommt eine Region-ID.
  Beispiele: "Leiste rechts" → "groin_right", "Lunge" → "lung",
  "Wirbelsäule" → "spine", "Thorax" → "thorax"
- "metastases_body": Alle Region-IDs aus metastases_detail gesammelt.
- Status-Regeln:
  * "inactive" wenn die Metastase/der Tumor reseziert/entfernt wurde (R0)
  * "responding" wenn unter Therapie eine Größenreduktion dokumentiert ist
  * "active" wenn die Metastase weiterhin vorhanden und nicht reseziert ist
- "primary_tumor_status": "inactive" wenn R0-Resektion erfolgte, sonst "active"

Regeln für Therapien:
- "surgery": Alle operativen Eingriffe (Exzision, Nachexzision, SLNE, CLND, Metastasenresektion)
- "radiation": Alle Strahlentherapien
- "systemic": Alle Systemtherapien (Immuntherapie, zielgerichtete Therapie, Chemotherapie)
- "current": Die aktuelle Therapie/Nachsorge zum Zeitpunkt der Konferenz

Antworte ausschließlich mit dem JSON-Objekt, kein zusätzlicher Text.
"""


def extract_clinical_data(protocol_text):
    """Call the LLM to extract structured clinical data from protocol text."""
    if not LLM_BASE_URL:
        print("ERROR: LLM_BASE_URL not set.  Cannot extract clinical data.")
        sys.exit(1)

    content = _llm_chat(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": f"Hier ist das Tumorkonferenz-Protokoll:\n\n{protocol_text}"},
        ],
        max_tokens=8192,
        json_mode=True,
    )
    return json.loads(content)


# ── imaging body_region enrichment ───────────────────────────────────────────

IMAGING_REGION_PROMPT = """Du erhältst eine Liste von bildgebenden Befunden als JSON.
Ergänze für jeden Befund das Feld "body_region" mit der passenden Region-ID
und das Feld "region" mit einer deutschen anatomischen Beschreibung.

Verwende NUR diese Region-IDs: """ + json.dumps(BODY_REGION_IDS) + """

Antworte mit einem JSON-Array, das dieselbe Struktur wie die Eingabe hat,
erweitert um "body_region" und "region" für jeden Eintrag.
Antworte ausschließlich mit dem JSON-Array.
"""


def enrich_imaging_regions(imaging_list):
    """Add body_region and region fields to imaging entries via LLM."""
    if not imaging_list or not LLM_BASE_URL:
        return imaging_list

    # Prepare a minimal payload for the LLM (just date, type, finding excerpt)
    summaries = []
    for img in imaging_list:
        summaries.append({
            'date': img.get('date', ''),
            'type': img.get('type', ''),
            'finding_excerpt': (img.get('finding', '') or '')[:500],
        })

    content = _llm_chat(
        messages=[
            {"role": "system", "content": IMAGING_REGION_PROMPT},
            {"role": "user",   "content": json.dumps(summaries, ensure_ascii=False)},
        ],
        max_tokens=8192,
        json_mode=True,
    )
    enriched = json.loads(content)
    # The model might wrap the array in an object
    if isinstance(enriched, dict):
        enriched = enriched.get('imaging', enriched.get('results', list(enriched.values())[0]))

    if not isinstance(enriched, list) or len(enriched) != len(imaging_list):
        print(f"  WARNING: imaging enrichment returned {len(enriched) if isinstance(enriched, list) else 'non-list'} items, expected {len(imaging_list)}.  Skipping.")
        return imaging_list

    for orig, enr in zip(imaging_list, enriched):
        orig['body_region'] = enr.get('body_region', '')
        orig['region'] = enr.get('region', '')

    return imaging_list


# ── imaging summarization ────────────────────────────────────────────────────

IMAGING_SUMMARY_PROMPT = """Du bist ein klinischer Dokumentationsassistent für ein Hauttumorzentrum.
Du erhältst eine Liste bildgebender Befundtexte eines Melanom-Patienten als JSON-Array.

Für JEDEN Eintrag im Input-Array:
1. "summary": Fasse den Befund in 1–3 kurzen Sätzen zusammen (max. 200 Zeichen).
   REGELN:
   - Nenne die wichtigsten Befunde: neue/progrediente Läsionen, Metastasen, Tumorgröße, relevante Nebenbefunde.
   - KEINE Wiederholung von Klinikangaben, Fragestellung oder Untersuchungsprotokoll.
   - KEINE Informationen ERFINDEN. Nur wiedergeben, was tatsächlich im Text steht.
   - Kurz und prägnant, wie eine Karteikarten-Notiz.
   - Wenn eine Beurteilung explizit im Text steht, diese bevorzugt zusammenfassen.

Antworte als JSON: {"entries": [{"index": 0, "summary": "..."}, ...]}
Antworte auf DEUTSCH.
"""


def summarize_imaging(imaging_list):
    """Summarize imaging findings via LLM. Adds 'summary' field to each entry."""
    if not imaging_list or not LLM_BASE_URL:
        return imaging_list

    payload = []
    for i, img in enumerate(imaging_list):
        finding = (img.get('finding', '') or '').strip()
        if not finding:
            continue
        payload.append({
            'index': i,
            'date': img.get('date', ''),
            'type': img.get('type', ''),
            'finding': finding[:2000],
        })

    if not payload:
        return imaging_list

    BATCH_SIZE = 10

    for batch_start in range(0, len(payload), BATCH_SIZE):
        batch = payload[batch_start:batch_start + BATCH_SIZE]
        try:
            content = _llm_chat(
                messages=[
                    {"role": "system", "content": IMAGING_SUMMARY_PROMPT},
                    {"role": "user",   "content": json.dumps(batch, ensure_ascii=False)},
                ],
                max_tokens=4096,
                json_mode=True,
            )
            parsed = json.loads(content)
            entries = parsed if isinstance(parsed, list) else parsed.get('entries', list(parsed.values())[0] if parsed else [])
            for item in entries:
                idx = item.get('index', None)
                if idx is not None and 0 <= idx < len(imaging_list):
                    imaging_list[idx]['summary'] = item.get('summary', '')
        except Exception as e:
            print(f"  WARNING: Imaging summarization batch failed: {e}")

    return imaging_list


# ── timeline summarization & categorization ──────────────────────────────────

TIMELINE_CATEGORIES = [
    "Diagnostik",
    "Therapie",
    "Nebenwirkung",
    "Tumorkonferenz",
    "Staging",
    "Nachsorge",
    "Konsil",
    "Sonstiges",
]

TIMELINE_SUMMARY_PROMPT = """Du bist ein klinischer Dokumentationsassistent für ein Hauttumorzentrum.
Du erhältst Verlaufseinträge eines Melanom-Patienten. Die Rohdaten enthalten viel wiederkehrenden Boilerplate-Text (Stammdaten, Kontaktinfos, Diagnosehistorie, Allergien, Dauermedikation), der bei JEDEM Eintrag wiederholt wird. Diesen Boilerplate sollst du KOMPLETT IGNORIEREN.

Für JEDEN Eintrag im Input-Array:

1. "summary": Fasse NUR die NEUE, KLINISCH RELEVANTE Information in 1–2 kurzen Sätzen zusammen (max. 100 Zeichen).
   REGELN:
   - Was ist das klinisch Neue bei DIESEM Termin? (Befund, Therapiegabe, Entscheidung, Komplikation)
   - NIEMALS Stammdaten, Tumorhistorie, Kontaktdaten, Allergien, Dauermedikation wiederholen
   - NIEMALS "Heute WV im HTZ", "Heute Vorstellung" o.ä. — direkt den Inhalt nennen
   - NIEMALS klinische Werte ERFINDEN. Wenn ein TNM-Stadium, Laborwert oder Befund NICHT EXPLIZIT im Text steht, NICHT hinzudichten. Nur das wiedergeben, was tatsächlich im Text steht.
   - Kurz und prägnant, wie eine Karteikarten-Notiz
   - In der Zusammenfassung KEINE Informationen aus anderen Kategorien mischen. Wenn der Eintrag Staging UND Molekularpathologie enthält, fasse NUR das Hauptereignis zusammen — NICHT beides.
   - KEINE Mutationsstatus (BRAF, NRAS, KIT) in Staging-Zusammenfassungen. KEIN Staging in Therapie-Zusammenfassungen.

2. "category": Genau EINE Kategorie zuweisen:

   "Therapie" → Jede Therapiegabe (z.B. "2. Gabe Nivolumab", "Pembrolizumab-Infusion"), OP, Bestrahlung, Therapiebeginn/-wechsel/-abbruch, Studieneinschluss
   "Diagnostik" → Bildgebung (CT, MRT, PET, Sono), Labor-Auswertung, Biopsie, Pathologie
   "Nebenwirkung" → irAE, Hypothyreose unter CPI, Toxizität, Therapiepause wegen NW, Dosisanpassung wegen NW
   "Tumorkonferenz" → HTK-Beschlüsse, Tumorboard-Empfehlungen, Tumorkonferenz-Protokolle
   "Staging" → Initiales Staging, Restaging-Ergebnis, Stadieneinteilung
   "Nachsorge" → Routine-Kontrolle OHNE neue Befunde oder Therapie, reine Nachsorge-WV
   "Konsil" → Fachärztliches Konsil (Kardioonkologie, Neurologie, Endokrinologie etc.)
   "Sonstiges" → NUR wenn keine der obigen Kategorien passt (rein administrative Einträge, Rezepte)

   WICHTIG: "Sonstiges" ist der LETZTE Ausweg. Wenn ein Eintrag eine Therapiegabe UND einen Laborbefund enthält, wähle die Kategorie des HAUPTEREIGNISSES.
   Ein Eintrag der "2. Gabe Nivolumab" erwähnt → "Therapie", NICHT "Sonstiges".
   Ein Eintrag mit Sonographie-Befund → "Diagnostik", NICHT "Sonstiges".
   Ein Eintrag über HTK-Empfehlung → "Tumorkonferenz", NICHT "Sonstiges".

Antworte als JSON: {"entries": [{"index": 0, "summary": "...", "category": "..."}, ...]}
Antworte auf DEUTSCH. Die Zusammenfassungen müssen auf Deutsch sein.
"""


def _strip_boilerplate(event_text):
    """Remove repeating boilerplate sections from raw EMR event text.

    EMR entries typically start with a block of repeated patient demographics,
    diagnosis history, contact info, allergies, and chronic meds that is
    copy-pasted into every visit note.  We strip those sections so the LLM
    only sees the novel clinical content.

    NOTE: This is the single-entry fallback.  Prefer _strip_cross_entry_boilerplate()
    when the full timeline is available (it detects boilerplate automatically).
    """
    if not event_text:
        return ''

    lines = event_text.splitlines()
    skip_patterns = [
        'malignes melanom td',
        'stadium bei ed',
        'hausarzt:',
        'hautarzt:',
        'notfallkontakt:',
        'sozialanamnese:',
        'allergien:',
        'dauermedikation',
        'melanomanamnese:',
        'heute wv im htz',
        'heute vorstellung',
        'heute erstvorstellung',
        'block-nr.',
        'histologische begutachtung',
        'behandlung durch xxx',
        'partner herr',
        'partner frau',
        'gelernte ',
        'lebt mit ',
    ]
    filtered = []
    for line in lines:
        low = line.strip().lower()
        if not low:
            continue
        if any(pat in low for pat in skip_patterns):
            continue
        filtered.append(line.strip())

    return '\n'.join(filtered)


def _strip_cross_entry_boilerplate(timeline, threshold=3):
    """Remove boilerplate lines detected by cross-entry frequency analysis.

    Approach:
    1. Normalize each line (lowercase, collapse whitespace).
    2. Count how many distinct entries each normalized line appears in.
    3. Lines appearing in >= *threshold* entries are classified as boilerplate.
    4. Return a list of cleaned event texts (same order as input).

    This catches all copy-pasted blocks (demographics, diagnosis history,
    medication, allergies, contact info, section headers, etc.) without
    relying on hand-crafted patterns.
    """
    from collections import Counter

    if len(timeline) < threshold:
        # Not enough entries to detect cross-entry repetition; fall back.
        return [_strip_boilerplate(e.get('event', '')) for e in timeline], ['' for _ in timeline]

    # --- Pass 1: count per-entry line frequency ---
    line_freq = Counter()          # normalized_line -> number of entries it appears in
    for entry in timeline:
        seen_in_entry = set()
        for line in entry.get('event', '').splitlines():
            norm = ' '.join(line.strip().lower().split())
            if not norm:
                continue
            if norm not in seen_in_entry:
                line_freq[norm] += 1
                seen_in_entry.add(norm)

    boilerplate = {ln for ln, cnt in line_freq.items() if cnt >= threshold}

    # --- Pass 2: strip boilerplate lines from each entry ---
    # Keep the first occurrence of each boilerplate line (it's the original
    # clinical documentation); only strip from subsequent entries.
    seen_boilerplate = set()       # boilerplate lines already seen in an earlier entry
    cleaned_texts = []
    removed_texts = []             # removed lines per entry (for UI display)
    for entry in timeline:
        kept = []
        removed = []
        for line in entry.get('event', '').splitlines():
            norm = ' '.join(line.strip().lower().split())
            if not norm:
                continue
            if norm in boilerplate:
                if norm not in seen_boilerplate:
                    # First occurrence — keep it
                    seen_boilerplate.add(norm)
                    kept.append(line.strip())
                else:
                    removed.append(line.strip())
                continue
            kept.append(line.strip())
        cleaned_texts.append('\n'.join(kept))
        removed_texts.append('\n'.join(removed))

    return cleaned_texts, removed_texts


def summarize_timeline(timeline):
    """Summarize and categorize timeline entries via LLM.

    Returns a list of dicts with 'summary' and 'category' for each entry.
    """
    if not timeline or not LLM_BASE_URL:
        return [{'summary': e.get('event', '')[:120], 'category': 'Sonstiges'} for e in timeline]

    results = [None] * len(timeline)

    # Strip boilerplate via cross-entry frequency analysis
    cleaned_texts, removed_texts = _strip_cross_entry_boilerplate(timeline)

    payload = []
    for i, (entry, cleaned) in enumerate(zip(timeline, cleaned_texts)):
        if not cleaned.strip():
            # Entry was entirely boilerplate — mark as empty, skip LLM
            results[i] = {'summary': '', 'category': '_empty', 'removed_lines': removed_texts[i], 'cleaned_event': ''}
            continue
        payload.append({
            'index': i,
            'date': entry.get('date', ''),
            'type': entry.get('type', ''),
            'event': cleaned[:1500],
        })

    # Smaller batches for better quality
    BATCH_SIZE = 10

    for batch_start in range(0, len(payload), BATCH_SIZE):
        batch = payload[batch_start:batch_start + BATCH_SIZE]
        try:
            content = _llm_chat(
                messages=[
                    {"role": "system", "content": TIMELINE_SUMMARY_PROMPT},
                    {"role": "user",   "content": json.dumps(batch, ensure_ascii=False)},
                ],
                max_tokens=4096,
                json_mode=True,
            )
            parsed = json.loads(content)
            entries = parsed if isinstance(parsed, list) else parsed.get('entries', list(parsed.values())[0] if parsed else [])

            for item in entries:
                idx = item.get('index', None)
                if idx is not None and 0 <= idx < len(timeline):
                    cat = item.get('category', 'Sonstiges')
                    if cat not in TIMELINE_CATEGORIES:
                        cat = 'Sonstiges'
                    results[idx] = {
                        'summary': item.get('summary', ''),
                        'category': cat,
                        'removed_lines': removed_texts[idx] if idx < len(removed_texts) else '',
                        'cleaned_event': cleaned_texts[idx] if idx < len(cleaned_texts) else '',
                    }
        except Exception as e:
            print(f"  WARNING: Timeline summarization batch {batch_start//BATCH_SIZE + 1} failed: {e}")

    # Fill any gaps
    for i, r in enumerate(results):
        if r is None:
            results[i] = {
                'summary': (timeline[i].get('event', '') or '')[:120],
                'category': 'Sonstiges',
                'removed_lines': removed_texts[i] if i < len(removed_texts) else '',
                'cleaned_event': cleaned_texts[i] if i < len(cleaned_texts) else '',
            }

    return results


def _extract_aktuelles_stadium(timeline):
    """Deterministically extract 'Aktuelles Stadium' from most recent timeline entry.

    Scans timeline entries (latest first) for a line like:
        Aktuelles Stadium nach AJCC 2017: pT2 cN cM1a, Stadium IV
    Returns a dict with t, n, m, uicc, classification keys, or None.
    Does NOT set 'date' — that line is repeated boilerplate, so the entry date
    is not when staging was determined.  The caller should keep the LLM date.
    """
    import re
    pattern = re.compile(
        r'Aktuelles\s+Stadium\s+(?:nach\s+)?'
        r'(AJCC\s*\d{4})?\s*:\s*'
        r'(p?T\S*)\s+(c?p?N\S*)\s+(c?p?M\S*)'
        r'(?:,?\s*(?:Stadium|Std\.?)\s*(\S+))?',
        re.IGNORECASE,
    )
    # Walk from most recent entry
    for entry in reversed(timeline or []):
        text = entry.get('event', '') or ''
        m = pattern.search(text)
        if m:
            result = {
                't': m.group(2),
                'n': m.group(3),
                'm': m.group(4).rstrip(','),
                'classification': (m.group(1) or '').strip(),
            }
            if m.group(5):
                result['uicc'] = m.group(5)
            return result
    return None


# ── assembly ─────────────────────────────────────────────────────────────────

def _therapy_date_key(entry):
    """Parse therapy dates into a sortable (year, month, day) tuple.

    Supported formats: DD.MM.YYYY, MM.YYYY, MM/YYYY, seit DD.MM.YYYY,
    seit MM/YYYY.
    """
    d = re.sub(r'^seit\s+', '', entry.get('date', '').strip())
    # DD.MM.YYYY
    m = re.match(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', d)
    if m:
        return (int(m.group(3)), int(m.group(2)), int(m.group(1)))
    # MM.YYYY
    m = re.match(r'^(\d{1,2})\.(\d{4})$', d)
    if m:
        return (int(m.group(2)), int(m.group(1)), 0)
    # MM/YYYY
    m = re.match(r'^(\d{1,2})/(\d{4})$', d)
    if m:
        return (int(m.group(2)), int(m.group(1)), 0)
    return (0, 0, 0)


def _sort_therapies(therapies):
    """Sort each therapy category list oldest-first (ascending)."""
    for key in ('surgery', 'radiation', 'systemic', 'other_locoregional', 'current'):
        if key in therapies and isinstance(therapies[key], list):
            therapies[key] = sorted(therapies[key], key=_therapy_date_key)
    return therapies


def build_dashboard_json(case_id, sources_dir, row=None):
    """Build a complete dashboard JSON for a case.

    Args:
        case_id:     Case identifier (hash or integer string).
        sources_dir: Directory containing source files.
        row:         Optional CSV row dict for demographic data.
    """
    print(f"Processing case {case_id}...")

    # 1. Read protocol text
    protocol = read_protocol_text(case_id, sources_dir)
    if not protocol:
        print(f"  WARNING: No protocol text found for {case_id}")
        return None

    # 2. LLM extraction of clinical data
    print("  Extracting clinical data via LLM...")
    clinical = extract_clinical_data(protocol)

    # 3. Deterministic parsers
    print("  Parsing timeline...")
    timeline = parse_verlaufsdoku(case_id, sources_dir)

    # Summarize & categorize timeline entries via LLM
    if timeline:
        print("  Summarizing timeline via LLM...")
        summaries = summarize_timeline(timeline)
        # Attach summaries and filter out empty entries (all-boilerplate)
        kept = []
        for entry, s in zip(timeline, summaries):
            if s['category'] == '_empty':
                continue
            entry['summary'] = s['summary']
            entry['category'] = s['category']
            entry['removed_lines'] = s.get('removed_lines', '')
            entry['cleaned_event'] = s.get('cleaned_event', entry.get('event', ''))
            kept.append(entry)
        timeline = kept

    print("  Parsing lab values...")
    lab_values = parse_lab_values(case_id, sources_dir)

    print("  Parsing imaging...")
    imaging = parse_imaging(case_id, sources_dir)
    if imaging:
        print("  Enriching imaging regions via LLM...")
        imaging = enrich_imaging_regions(imaging)
        print("  Summarizing imaging findings via LLM...")
        imaging = summarize_imaging(imaging)

    # 4. Override demographics from CSV if available
    if row:
        gender_raw = row.get('p0.gender', '')
        gender_map = {
            'male': 'M', 'female': 'W', 'm': 'M', 'f': 'W',
            'männlich': 'M', 'weiblich': 'W',
        }
        sex_code = gender_map.get(gender_raw.lower(), clinical.get('sex', ''))
        birth_date = row.get('p0.birthDate', '')
        issued_date = row.get('d0_issued', '')
        age = _calculate_age(birth_date, issued_date)
        dob_display = _format_date(birth_date) if birth_date else clinical.get('dob', '')
        family_name = row.get('pn0.family', '')

        if family_name:
            clinical['name'] = family_name
        if sex_code:
            clinical['sex'] = sex_code
        if dob_display:
            clinical['dob'] = dob_display
        if age != '':
            clinical['age'] = age

    # 5. Collect imaged_regions from imaging body_regions
    imaged_regions = []
    for img in imaging:
        br = img.get('body_region', '')
        if br and br not in imaged_regions:
            imaged_regions.append(br)

    # 5b. Deterministic staging override from timeline raw text
    #     The LLM sometimes infers staging from disease course instead of
    #     reading "Aktuelles Stadium" literally.  We parse the most recent
    #     occurrence of that line from the timeline entries.
    _staging_override = _extract_aktuelles_stadium(timeline)
    if _staging_override:
        clinical['staging'] = {**clinical.get('staging', {}), **_staging_override}

    # If staging date is still empty, use date of most recent staging-categorized entry
    staging = clinical.get('staging', {})
    if not staging.get('date'):
        for entry in reversed(timeline or []):
            if entry.get('category') == 'Staging' and entry.get('date'):
                staging['date'] = entry['date']
                break

    # 6. Assemble final dict
    dashboard = {
        # Demographics
        'name':              clinical.get('name', ''),
        'dob':               clinical.get('dob', ''),
        'age':               clinical.get('age', ''),
        'sex':               clinical.get('sex', ''),
        'location':          clinical.get('location', ''),
        'pat_id':            clinical.get('pat_id', ''),

        # Clinical
        'comorbidities':     clinical.get('comorbidities', []),
        'ecog':              clinical.get('ecog', ''),
        'karnofsky':         clinical.get('karnofsky', ''),

        # Primary tumor
        'primary_diagnosis': clinical.get('primary_diagnosis', ''),
        'primary_location':  clinical.get('primary_location', ''),
        'diagnosis_date':    clinical.get('diagnosis_date', ''),
        'tumor_thickness':   clinical.get('tumor_thickness', ''),
        'ulceration':        clinical.get('ulceration', ''),
        'mitoses':           clinical.get('mitoses', ''),
        'histology':         clinical.get('histology', ''),
        'resection_status':  clinical.get('resection_status', ''),
        'safety_margin':     clinical.get('safety_margin', ''),
        'slne':              clinical.get('slne', ''),

        # Staging
        'initial_staging':   clinical.get('initial_staging', {}),
        'staging':           clinical.get('staging', {}),

        # Molecular pathology
        'mutations':         clinical.get('mutations', {}),
        'pdl1':              clinical.get('pdl1', ''),
        'hla_a':             clinical.get('hla_a', ''),

        # Body map
        'primary_tumor_body':      clinical.get('primary_tumor_body', []),
        'primary_tumor_status':    clinical.get('primary_tumor_status', 'inactive'),
        'primary_inactive_reason': clinical.get('primary_inactive_reason', ''),
        'metastases_body':         clinical.get('metastases_body', []),
        'metastases_detail':       clinical.get('metastases_detail', []),
        'imaged_regions':          imaged_regions,

        # Dynamic data
        'timeline':    timeline,
        'lab_values':  lab_values,
        'imaging':     imaging,

        # Therapies – sort each category oldest-first (ascending by DD.MM.YYYY)
        'therapies':       _sort_therapies(clinical.get('therapies', {
            'surgery': [], 'radiation': [], 'systemic': [], 'current': [],
        })),

        # Status
        'disease_status':  clinical.get('disease_status', ''),
    }

    return dashboard


# ── case discovery ───────────────────────────────────────────────────────────

def discover_cases_external():
    """Discover cases from CSV.  Returns list of (case_id, row_dict)."""
    if not CSV_PATH or not os.path.isfile(CSV_PATH):
        return []
    with open(CSV_PATH, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    cases = []
    for row in rows:
        case_id = row.get('d0.id', '').strip()
        if case_id:
            cases.append((case_id, row))
    return cases


def discover_cases_legacy():
    """Discover legacy cases from original_documents/.  Returns list of (case_id, None)."""
    cases = []
    if not os.path.isdir(DOCUMENTS_DIR):
        return cases
    for fname in os.listdir(DOCUMENTS_DIR):
        if not fname.lower().endswith('.txt'):
            continue
        m = re.match(r'[Ff]all(\d+)', fname)
        if m:
            cases.append((m.group(1), None))
    cases.sort(key=lambda x: int(x[0]))
    return cases


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Pre-process case data into dashboard JSON.')
    parser.add_argument('--case', type=str, help='Process a single case ID')
    parser.add_argument('--legacy', action='store_true', help='Process legacy Fall1..5 cases')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing files')
    parser.add_argument('--output-dir', type=str, default=None,
                        help='Output directory (default: dashboard_data/)')
    args = parser.parse_args()

    output_dir = args.output_dir or DASHBOARD_DIR
    os.makedirs(output_dir, exist_ok=True)

    # Determine sources directory and cases
    if args.legacy:
        sources_dir = DOCUMENTS_DIR  # legacy uses original_documents/ as source
        all_cases = discover_cases_legacy()
        sources_dir = ''  # protocol text is in DOCUMENTS_DIR, handled by read_protocol_text
    elif DATA_ROOT and os.path.isdir(DATA_ROOT):
        sources_dir = SOURCES_DIR
        all_cases = discover_cases_external()
    else:
        print("No DATA_ROOT configured and --legacy not specified.")
        print("Set DATA_ROOT in .env or use --legacy for Fall1..5 cases.")
        sys.exit(1)

    if args.case:
        all_cases = [(cid, row) for cid, row in all_cases if cid == args.case]
        if not all_cases:
            print(f"Case {args.case} not found.")
            sys.exit(1)

    print(f"Found {len(all_cases)} case(s) to process.")
    print(f"Output directory: {output_dir}")
    print()

    for case_id, row in all_cases:
        out_path = os.path.join(output_dir, f'{case_id}_dashboard.json')

        # Skip if already exists (incremental)
        if os.path.isfile(out_path) and not args.case:
            print(f"Skipping {case_id} (already exists).  Use --case {case_id} to reprocess.")
            continue

        dashboard = build_dashboard_json(case_id, sources_dir or SOURCES_DIR, row)
        if dashboard is None:
            print(f"  SKIPPED (no data)")
            continue

        if args.dry_run:
            print(f"  [DRY RUN] Would write {out_path}")
            print(f"  Keys: {list(dashboard.keys())}")
            print(f"  Timeline entries: {len(dashboard.get('timeline', []))}")
            print(f"  Lab values: {len(dashboard.get('lab_values', []))}")
            print(f"  Imaging: {len(dashboard.get('imaging', []))}")
            print(f"  Metastases: {len(dashboard.get('metastases_detail', []))}")
            print(f"  Body regions: primary={dashboard.get('primary_tumor_body')}, "
                  f"meta={dashboard.get('metastases_body')}")
        else:
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(dashboard, f, indent=2, ensure_ascii=False)
            print(f"  Wrote {out_path}")

        print()

    print("Done.")


if __name__ == '__main__':
    main()
