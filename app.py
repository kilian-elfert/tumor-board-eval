from flask import (Flask, render_template, request, redirect, url_for,
                   session, send_file, jsonify)
import json, csv, io, os, random, threading, traceback
import urllib.request
from werkzeug.security import check_password_hash
from functools import wraps
from datetime import datetime
from markupsafe import Markup, escape
from dotenv import load_dotenv
import re as _re

load_dotenv(override=True)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-in-production-please')

_KA_SYNONYMS = {'nicht angegeben', 'nicht bestimmt', 'nicht bekannt', 'unbekannt', 'n/a', ''}

@app.template_filter('ka')
def _keine_angabe(value):
    """Replace empty / placeholder values with 'Keine Angabe'."""
    if not value or str(value).strip().lower() in _KA_SYNONYMS:
        return 'Keine Angabe'
    return value

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
TEXTS_FILE      = os.path.join(BASE_DIR, 'texts.json')
RESPONSES_DIR   = os.path.join(BASE_DIR, 'responses')
USERS_FILE      = os.path.join(BASE_DIR, 'users.json')
GUIDELINE_DIR   = os.path.join(BASE_DIR, 'guideline')
TEXTS_HUMAN_DIR = os.path.join(BASE_DIR, 'texts_human')
TEXTS_LLM_DIR   = os.path.join(BASE_DIR, 'texts_llm')
EXPORTS_DIR     = os.path.join(BASE_DIR, 'exports')
HIGHLIGHT_FILE  = os.path.join(BASE_DIR, 'highlight_mappings.json')
ANNOTATIONS_DIR = os.path.join(BASE_DIR, 'annotations')
DASHBOARD_DIR   = os.path.join(BASE_DIR, 'dashboard_data')

# External data root (originals + imaging reports). Configured via .env:
#   DATA_ROOT       absolute path, e.g.  C:\Users\kilia\Desktop\Data
#   SOURCES_SUBDIR  subdirectory holding sources (default: 'sources')
# Falls back to in-repo 'original_documents' / 'imaging' for backwards
# compatibility if the external path is not configured / does not exist.
_DATA_ROOT      = os.path.expanduser(os.environ.get('DATA_ROOT', '').strip())
_SOURCES_SUBDIR = os.environ.get('SOURCES_SUBDIR', 'sources').strip()
_SOURCES_DIR    = os.path.join(_DATA_ROOT, _SOURCES_SUBDIR) if _DATA_ROOT else ''

if _SOURCES_DIR and os.path.isdir(_SOURCES_DIR):
    DOCUMENTS_DIR = _SOURCES_DIR   # holds <case_id>.{pdf,txt}
    IMAGING_DIR   = _SOURCES_DIR   # holds <case_id>_<modality>/...
else:
    DOCUMENTS_DIR = os.path.join(BASE_DIR, 'original_documents')
    IMAGING_DIR   = os.path.join(BASE_DIR, 'imaging')

# Case-id heuristic: 64-char lowercase hex (SHA-256 stem). Used to filter
# auxiliary files in shared sources directories.
import re as _re
_CASE_ID_RE = _re.compile(r'^[0-9a-f]{64}$')
def _is_case_id(s: str) -> bool:
    return bool(_CASE_ID_RE.match(s or ''))


# ── push notifications via ntfy.sh ───────────────────────────────────────────

NTFY_URL = os.environ.get('NTFY_URL', '').strip()  # e.g. https://ntfy.sh/my-topic


def _send_notification_email(subject, body):
    """Send a push notification via ntfy.sh (no login, no mail server).

    Set NTFY_URL in .env, e.g.:
        NTFY_URL=https://ntfy.sh/my-secret-topic

    Silently no-ops if NTFY_URL is unset. Runs in a daemon thread so failures
    never affect request handling.
    """
    if not NTFY_URL:
        return

    def _worker():
        try:
            req = urllib.request.Request(
                NTFY_URL,
                data=body.encode(),
                headers={'Title': subject, 'Content-Type': 'text/plain; charset=utf-8'},
                method='POST',
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            try:
                app.logger.warning('ntfy notification failed:\n%s', traceback.format_exc())
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()

# ── constants ────────────────────────────────────────────────────────────────

INFO_ITEMS = [
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
    "Aktueller Status: Stadium der Erkrankung",
    "Aktueller Status: Metastasierung",
    "Aktueller Status: Lokalisation der Metastasierung",
    "Aktueller Status: Symptome und Beschwerden",
    "Entscheidungsrelevante Faktoren: Patientenpräferenzen",
    "Entscheidungsrelevante Faktoren: Behandlungsziel",
    "Entscheidungsrelevante Faktoren: Beschluss der letzten Tumorkonferenz",
]

# section  = display name in stepper
# subtitle = short description
# key      = storage key
# tab      = True means show tab A / tab B; False = single form (side-by-side or no text)
# section_type = 'summary' | 'problem' | 'both'
RATING_STEPS = [
    {
        "index": 1,
        "key":   "summary_falseinfo",
        "section": "Zusammenfassung",
        "subtitle": "Falschinformationen",
        "has_tabs": True,
        "section_type": "summary",
        "alert": "Bewerten Sie für jede vom Annotator als inhaltlich falsch oder irreführend markierte Information die möglichen Folgen und ihre Eintrittswahrscheinlichkeit.",
    },
    {
        "index": 1,
        "key":   "summary_missinginfo",
        "section": "Zusammenfassung",
        "subtitle": "Fehlende Informationen",
        "has_tabs": True,
        "section_type": "summary",
        "alert": "Bewerten Sie für jede klinisch relevante, in dieser Version aber fehlende Information die möglichen Folgen und ihre Eintrittswahrscheinlichkeit.",
    },
    {
        "index": 2,
        "key":   "summary_correctness",
        "section": "Zusammenfassung",
        "subtitle": "Korrektheit",
        "has_tabs": True,
        "section_type": "summary",
        "alert": "Beurteilen Sie die Korrektheit der Zusammenfassung. Sind alle enthaltenen Informationen medizinisch korrekt und frei von Ungenauigkeiten?",
        "scale_labels": [
            "Sehr schlecht",
            "Schlecht",
            "Akzeptabel",
            "Gut",
            "Ausgezeichnet"
        ],
        "scale_descriptions": [
            "Enthält schwerwiegende Ungenauigkeiten oder irreführende Informationen.",
            "Mehrere Fehler, die zu Fehlinterpretationen führen könnten.",
            "Einige kleinere Ungenauigkeiten, aber insgesamt verständlich.",
            "Weitgehend korrekt, nur gelegentlich kleinere Fehler.",
            "Vollständig korrekt, keine erkennbaren Fehler."
        ],
    },
    {
        "index": 3,
        "key":   "summary_completeness",
        "section": "Zusammenfassung",
        "subtitle": "Vollständigkeit",
        "has_tabs": True,
        "section_type": "summary",
        "alert": "Beurteilen Sie, wie vollständig die Zusammenfassung alle klinisch relevanten Informationen des Falles abbildet.",
        "scale_labels": [
            "Sehr schlecht",
            "Schlecht",
            "Akzeptabel",
            "Gut",
            "Ausgezeichnet"
        ],
        "scale_descriptions": [
            "Lässt die meisten wesentlichen klinischen Details aus; die Zusammenfassung ist unvollständig.",
            "Es fehlen mehrere wichtige Details, sodass der Fall schwer zu verstehen ist.",
            "Enthält einige wichtige Details, aber es fehlt wichtiger Kontext.",
            "Erfasst die meisten wesentlichen Details mit nur geringfügigen Auslassungen.",
            "Bietet eine gründliche, umfassende Zusammenfassung mit allen kritischen Details."
        ],
    },
    {
        "index": 4,
        "key":   "summary_conciseness",
        "section": "Zusammenfassung",
        "subtitle": "Prägnanz",
        "has_tabs": True,
        "section_type": "summary",
        "alert": "Beurteilen Sie, ob die Zusammenfassung angemessen prägnant ist – weder zu knapp noch zu ausführlich.",
        "scale_labels": [
            "Sehr schlecht",
            "Schlecht",
            "Akzeptabel",
            "Gut",
            "Ausgezeichnet"
        ],
        "scale_descriptions": [
            "Übermäßig lang und mit irrelevanten Details überladen.",
            "Enthält unnötige Informationen, die von den Kernpunkten ablenken.",
            "Einigermaßen fokussiert, könnte aber prägnanter sein.",
            "Überwiegend prägnant, mit nur geringfügig überflüssigen Details.",
            "Sehr fokussiert, enthält nur die für die Klarheit notwendigen Details."
        ],
    },
    {
        "index": 5,
        "key":   "summary_postedit",
        "section": "Zusammenfassung",
        "subtitle": "Bearbeitungsaufwand",
        "has_tabs": True,
        "section_type": "summary",
        "alert": "Schätzen Sie den Aufwand, den Sie benötigen würden, um diese Zusammenfassung für den klinischen Einsatz zu überarbeiten.",
        "scale_labels": [
            "Sehr gering",
            "Gering",
            "Mittel",
            "Hoch",
            "Sehr hoch"
        ],
    },
    {
        "index": 6,
        "key":   "summary_origin_guess",
        "section": "Zusammenfassung",
        "subtitle": "Ursprung",
        "has_tabs": True,
        "section_type": "summary",
        "alert": "Was glauben Sie: Wurde diese Zusammenfassung von einem Menschen oder von einer KI verfasst?",
        "options": ["Mensch", "Unsicher", "KI"],
    },
    {
        "index": 7,
        "key":   "summary_preference",
        "section": "Zusammenfassung",
        "subtitle": "Präferenz",
        "has_tabs": False,
        "section_type": "summary",
        "alert": "Vergleichen Sie beide Versionen der Zusammenfassung und wählen Sie, welche Version Sie für den klinischen Einsatz bevorzugen würden.",
        "options": ["Version A", "Keine Präferenz", "Version B"],
    },
    {
        "index": 8,
        "key":   "problem_focus",
        "section": "Fragestellung",
        "subtitle": "Fokus",
        "has_tabs": True,
        "section_type": "problem",
        "alert": "Wählen Sie die in der Fragestellung vorgeschlagenen Diskussionsschwerpunkte.",
        "topic_options": [
            "Weitere Diagnostik",
            "Therapie (Beginn, Auswahl, Modifikation)",
            "Nachsorge",
            "Organisatorische Fragen",
            "Keine Spezifizierung"
        ],
    },
    {
        "index": 9,
        "key":   "problem_correctness",
        "section": "Fragestellung",
        "subtitle": "Nachvollziehbarkeit",
        "has_tabs": True,
        "section_type": "problem",
        "alert": "Beurteilen Sie die medizinische Nachvollziehbarkeit der Empfehlung in der Fragestellung.",
        "options": [
            "Entspricht dem aktuellen Therapiestandard (i.e., \"best-practice\")",
            "Entspricht nicht dem aktuellen Therapiestandard (i.e., \"not best-practice\"), ist aber medizinisch nachvollziehbar",
            "Medizinisch nicht nachvollziehbar",
            "Nicht spezifisch genug für eine Beurteilung",
        ],
    },
    {
        "index": 10,
        "key":   "problem_specificity",
        "section": "Fragestellung",
        "subtitle": "Fallspezifität",
        "has_tabs": True,
        "section_type": "problem",
        "alert": "Beurteilen Sie, ob die Fragestellung alle relevanten Aspekte des Falls berücksichtigt.",
        "options": ["Trifft zu", "Trifft teilweise zu", "Trifft nicht zu"],
    },
    {
        "index": 11,
        "key":   "problem_postedit",
        "section": "Fragestellung",
        "subtitle": "Bearbeitungsaufwand",
        "has_tabs": True,
        "section_type": "problem",
        "alert": "Schätzen Sie den Aufwand, den Sie benötigen würden, um diese Fragestellung für den klinischen Einsatz zu überarbeiten.",
        "scale_labels": [
            "Sehr gering",
            "Gering",
            "Mittel",
            "Hoch",
            "Sehr hoch"
        ],
    },
    {
        "index": 12,
        "key":   "problem_origin_guess",
        "section": "Fragestellung",
        "subtitle": "Ursprung",
        "has_tabs": True,
        "section_type": "problem",
        "alert": "Was glauben Sie: Wurde diese Fragestellung von einem Menschen oder von einer KI verfasst?",
        "options": ["Mensch", "Unsicher", "KI"],
    },
    {
        "index": 13,
        "key":   "problem_preference",
        "section": "Fragestellung",
        "subtitle": "Präferenz",
        "has_tabs": False,
        "section_type": "problem",
        "alert": "Vergleichen Sie beide Versionen der Fragestellung und wählen Sie, welche Sie für den klinischen Einsatz bevorzugen würden.",
        "options": ["Version A", "Keine Präferenz", "Version B"],
    },
    {
        "index": 14,
        "key":   "final_overall",
        "section": "Abschluss",
        "subtitle": "Fallkomplexität",
        "has_tabs": False,
        "section_type": "both",
        "alert": "Abschließende Bewertung des Falls: Bitte schätzen Sie die klinische Komplexität / Schwere des Falls und hinterlassen Sie ggf. einen Kommentar.",
    },
]

PROBLEM_FOCUS_STATUS_OPTIONS = ["Unverändert", "Progredient", "Remission"]
PROBLEM_FOCUS_TOPIC_OPTIONS  = [
    "Weitere Diagnostik",
    "Therapie (Beginn, Auswahl, Modifikation)",
    "Nachsorge",
    "Organisatorische Fragen",
    "Keine Spezifizierung",
]


def slugify(label):
    return ''.join(ch.lower() if ch.isalnum() else '_' for ch in label)


_SECTION_HEADER_RE = _re.compile(
    r'^(\d+\.\s+(?:Basisdaten|Aktuelles Staging|Verlauf|Wichtige Befunde))(.*)$',
    _re.MULTILINE,
)


def _bold_headers(text):
    """Escape text for HTML and wrap known section headers in <strong>."""
    if not text:
        return text
    safe = str(escape(text))
    safe = _SECTION_HEADER_RE.sub(r'<strong>\1</strong>\2', safe)
    return Markup(safe)


def _load_highlight_mappings():
    """Load pre-generated highlight mappings (slug → [excerpt, …]).

    If a researcher annotation file exists at ``annotations/<case_id>.json``,
    its span data overrides the LLM-generated mapping for that case.
    """
    if os.path.exists(HIGHLIGHT_FILE):
        with open(HIGHLIGHT_FILE, 'r', encoding='utf-8') as f:
            base = json.load(f)
    else:
        base = {}

    # Overlay researcher annotations
    if os.path.isdir(ANNOTATIONS_DIR):
        for fn in os.listdir(ANNOTATIONS_DIR):
            if not fn.endswith('.json'):
                continue
            case_id = fn[:-5]
            try:
                with open(os.path.join(ANNOTATIONS_DIR, fn), 'r', encoding='utf-8') as f:
                    ann = json.load(f)
            except Exception:
                continue
            case_block = base.setdefault(case_id, {})
            for text_key, payload in (ann.get('texts') or {}).items():
                target = case_block.setdefault(text_key, {})
                for slug, item in (payload.get('items') or {}).items():
                    spans = item.get('spans') or []
                    if spans:
                        target[slug] = list(spans)
    return base


def _annotation_path(case_id):
    return os.path.join(ANNOTATIONS_DIR, f'{case_id}.json')


def _seed_annotation_from_llm(case_id):
    """Build a fresh annotation skeleton pre-populated with the LLM's
    highlight suggestions (status='enthalten' for every span the LLM picked).

    Annotator can then accept (keep), reject (switch to falsch), remove, or add
    further spans. ``seeded_from_llm`` prevents re-seeding on later loads.
    """
    # Map annotator text keys → keys used in highlight_mappings.json
    src_for = {
        'human_summary': 'human_summary',
        'llm_summary':   'llm_summary',
    }
    skeleton = {
        'schema_version': 1,
        'case_id': case_id,
        'annotator': None,
        'updated_at': None,
        'seeded_from_llm': True,
        'texts': {tk: {'items': {}} for tk in src_for},
    }
    if not os.path.exists(HIGHLIGHT_FILE):
        return skeleton
    try:
        with open(HIGHLIGHT_FILE, 'r', encoding='utf-8') as f:
            hl = json.load(f)
    except Exception:
        return skeleton
    case_hl = hl.get(case_id) or {}
    for tk, src_key in src_for.items():
        seeded = case_hl.get(src_key) or {}
        items = skeleton['texts'][tk]['items']
        for slug, spans in seeded.items():
            spans = [s for s in (spans or []) if s]
            if spans:
                items[slug] = {'status': 'enthalten', 'spans': spans,
                               'origin': 'llm_seed'}
    return skeleton


def _load_annotation(case_id):
    """Load a researcher annotation, seeding from LLM highlights on first load."""
    p = _annotation_path(case_id)
    if os.path.exists(p):
        try:
            with open(p, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return _seed_annotation_from_llm(case_id)


def _save_annotation(case_id, data, username=None):
    os.makedirs(ANNOTATIONS_DIR, exist_ok=True)
    data['case_id'] = case_id
    data['schema_version'] = data.get('schema_version', 1)
    data['updated_at'] = datetime.utcnow().isoformat()
    if username:
        data['annotator'] = username
    with open(_annotation_path(case_id), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _annotate_highlights(raw_text, mapping, extra_class=''):
    """Apply _bold_headers then wrap mapped excerpts in <span> elements.

    *mapping* is {slug: [excerpt, …]} where excerpts are from the raw text.
    *extra_class* is appended to the span class list (e.g. ``hl-info--error``).
    Returns Markup.
    """
    if not raw_text or not mapping:
        return _bold_headers(raw_text)

    # Step 1: collect (start, end, slug) on the *raw* text
    regions = []
    for slug, excerpts in mapping.items():
        for exc in excerpts:
            idx = raw_text.find(exc)
            if idx != -1:
                regions.append((idx, idx + len(exc), slug))

    if not regions:
        return _bold_headers(raw_text)

    # Step 2: sort by start; merge overlapping regions, tracking all slugs
    regions.sort(key=lambda r: (r[0], -(r[1] - r[0])))
    merged = []  # list of (start, end, [slugs])
    for start, end, slug in regions:
        if merged and start < merged[-1][1]:
            # Overlap — add slug to existing region and extend end if needed
            prev_start, prev_end, prev_slugs = merged[-1]
            if slug not in prev_slugs:
                prev_slugs.append(slug)
            merged[-1] = (prev_start, max(prev_end, end), prev_slugs)
        else:
            merged.append((start, end, [slug]))

    # Step 3: build output by splicing raw text with <span> wrappers,
    #         escaping each segment individually so tags stay intact.
    parts = []
    prev = 0
    for start, end, slugs in merged:
        primary_slug = slugs[0]
        # Text before this region
        parts.append(str(escape(raw_text[prev:start])))
        # The highlighted region — use first slug as primary
        escaped_excerpt = str(escape(raw_text[start:end]))
        slug_attr = f'data-hl-slug="{primary_slug}"'
        if len(slugs) > 1:
            all_slugs = ','.join(slugs)
            slug_attr += f' data-hl-slugs="{all_slugs}"'
        cls = 'hl-info' + (f' {extra_class}' if extra_class else '')
        parts.append(
            f'<span class="{cls}" {slug_attr}>{escaped_excerpt}</span>'
        )
        # Add numbered footnote badges for every slug in an overlap
        if len(slugs) > 1:
            for i, fn_slug in enumerate(slugs, start=1):
                label = fn_slug.replace('__', ': ').replace('_', ' ')
                parts.append(
                    f'<sup class="hl-footnote" data-hl-slug="{fn_slug}" '
                    f'title="{str(escape(label))}">'
                    f'{i}</sup>'
                )
        prev = end
    parts.append(str(escape(raw_text[prev:])))

    html = ''.join(parts)
    # Bold section headers (regex works on the joined HTML)
    html = _SECTION_HEADER_RE.sub(r'<strong>\1</strong>\2', html)
    return Markup(html)


# ── helpers ──────────────────────────────────────────────────────────────────

def load_json(path, default=None):
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default if default is not None else {}


def save_json(path, data):
    """Atomically write JSON to ``path`` (write to a temp file, then rename).

    Concurrent POSTs (e.g. parallel saves from the dashboard relevance bulk
    toggle) used to race here and could leave the file with two concatenated
    JSON documents, breaking subsequent ``json.load`` calls.
    """
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = f'{path}.tmp.{os.getpid()}.{threading.get_ident()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_text_file(path):
    """Read a UTF-8 text file and return its content, or empty string."""
    if path and os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    return ''


_TEXT_SUBDIRS = (
    (TEXTS_HUMAN_DIR, 'zusammenfassung'),
    (TEXTS_HUMAN_DIR, 'fragestellung'),
    (TEXTS_LLM_DIR,   'zusammenfassung'),
    (TEXTS_LLM_DIR,   'fragestellung'),
)


def _discover_cases():
    """Discover local cases by case_id (hash or any string identifier).

    A case is any <case_id> for which at least one of these files exists:
      - texts_human/zusammenfassung/<case_id>.txt
      - texts_human/fragestellung/<case_id>.txt
      - texts_llm/zusammenfassung/<case_id>.txt
      - texts_llm/fragestellung/<case_id>.txt
      - original_documents/<case_id>.txt
      - original_documents/<case_id>.pdf
      - dashboard_data/<case_id>_dashboard.json

    Returns a sorted list of case_id strings.
    """
    ids: set[str] = set()

    # Text directories
    for base, sub in _TEXT_SUBDIRS:
        d = os.path.join(base, sub)
        if os.path.isdir(d):
            for fname in os.listdir(d):
                if fname.lower().endswith('.txt'):
                    ids.add(os.path.splitext(fname)[0])

    # Original documents (txt or pdf). When DOCUMENTS_DIR points at an external
    # sources/ folder it may also contain auxiliary files (e.g. <case_id>_lab.txt,
    # <case_id>_verlaufsdoku.jsonl) and modality subdirs — filter to bare
    # <case_id>.{txt,pdf} only by requiring the stem to be a 64-char hex hash.
    if os.path.isdir(DOCUMENTS_DIR):
        for fname in os.listdir(DOCUMENTS_DIR):
            stem, ext = os.path.splitext(fname)
            if ext.lower() in ('.txt', '.pdf') and _is_case_id(stem):
                ids.add(stem)

    # Dashboard JSONs (<case_id>_dashboard.json)
    if os.path.isdir(DASHBOARD_DIR):
        for fname in os.listdir(DASHBOARD_DIR):
            if fname.lower().endswith('_dashboard.json'):
                ids.add(fname[:-len('_dashboard.json')])

    return sorted(ids)


def _case_text_path(base, sub, case_id):
    """Return path to <base>/<sub>/<case_id>.txt if it exists, else None."""
    p = os.path.join(base, sub, f'{case_id}.txt')
    return p if os.path.isfile(p) else None


def _case_pdf_filename(case_id):
    """Return the basename of the protocol PDF for ``case_id`` in ``DOCUMENTS_DIR``.

    Resolution order:
      1. ``<case_id>.pdf`` (legacy / un-redacted)
      2. ``<case_id>_geschwärzt.pdf`` (redacted master copy)
      3. any ``<case_id>*.pdf`` (e.g. additional suffix variants), picked deterministically
    Returns the empty string if no match is found.
    """
    if not os.path.isdir(DOCUMENTS_DIR):
        return ''
    direct = f'{case_id}.pdf'
    if os.path.isfile(os.path.join(DOCUMENTS_DIR, direct)):
        return direct
    redacted = f'{case_id}_geschwärzt.pdf'
    if os.path.isfile(os.path.join(DOCUMENTS_DIR, redacted)):
        return redacted
    prefix = f'{case_id}'
    matches = sorted(
        f for f in os.listdir(DOCUMENTS_DIR)
        if f.lower().endswith('.pdf') and f.startswith(prefix)
    )
    return matches[0] if matches else ''


def load_texts():
    """Build case list from local texts_human/ and texts_llm/ folders."""
    case_ids = _discover_cases()
    if not case_ids:
        return load_json(TEXTS_FILE, [])

    texts = []
    for case_id in case_ids:
        pdf_name = _case_pdf_filename(case_id)
        texts.append({
            'id': case_id,
            'pdf': pdf_name,
            'human_summary': _read_text_file(_case_text_path(TEXTS_HUMAN_DIR, 'zusammenfassung',  case_id)),
            'human_problem': _read_text_file(_case_text_path(TEXTS_HUMAN_DIR, 'fragestellung',   case_id)),
            'llm_summary':   _read_text_file(_case_text_path(TEXTS_LLM_DIR,   'zusammenfassung', case_id)),
            'llm_problem':   _read_text_file(_case_text_path(TEXTS_LLM_DIR,   'fragestellung',   case_id)),
        })
    return texts


def _responses_path(username):
    """Return the path to a per-user responses file."""
    os.makedirs(RESPONSES_DIR, exist_ok=True)
    return os.path.join(RESPONSES_DIR, f'responses_{username}.json')


# Per-user lock so concurrent POSTs (e.g. parallel /api/case-relevance/<id>
# requests fired by the dashboard rail's bulk-set "alle ✓ / alle ✗") cannot
# interleave a read-modify-write on the same responses file.
_RESPONSES_LOCKS_GUARD = threading.Lock()
_RESPONSES_LOCKS: dict[str, threading.Lock] = {}


def _responses_lock(username):
    with _RESPONSES_LOCKS_GUARD:
        lock = _RESPONSES_LOCKS.get(username)
        if lock is None:
            lock = threading.Lock()
            _RESPONSES_LOCKS[username] = lock
        return lock


def load_responses(username):
    """Load a single user's response data (dict).  Returns {} if not found."""
    return load_json(_responses_path(username), {})


def save_responses(username, data):
    """Save a single user's response data, with texts embedded."""
    # Embed case texts so the response file is self-contained
    if 'texts' not in data:
        texts = load_texts()
        text_dict = {}
        for t in texts:
            cid = str(t['id'])
            text_dict[cid] = {
                'human_summary': t.get('human_summary', ''),
                'llm_summary':   t.get('llm_summary', ''),
                'human_problem': t.get('human_problem', ''),
                'llm_problem':   t.get('llm_problem', ''),
            }
        data['texts'] = text_dict
    save_json(_responses_path(username), data)


def load_all_responses():
    """Load all per-user response files into a {username: data} dict."""
    os.makedirs(RESPONSES_DIR, exist_ok=True)
    all_responses = {}
    for fname in os.listdir(RESPONSES_DIR):
        if fname.startswith('responses_') and fname.endswith('.json'):
            uname = fname[len('responses_'):-len('.json')]
            all_responses[uname] = load_json(os.path.join(RESPONSES_DIR, fname), {})
    return all_responses


def get_text_for_tab(case, section_type, assignment_summary, assignment_problem, tab_idx):
    """Return (text_content, version_label) for the given tab (0=A, 1=B)."""
    if section_type == 'summary':
        assignment = assignment_summary
        if tab_idx == 0:
            t = 'human' if assignment == 'human_first' else 'llm'
        else:
            t = 'llm' if assignment == 'human_first' else 'human'
        return (case['human_summary'] if t == 'human' else case['llm_summary'],
                'A' if tab_idx == 0 else 'B')
    elif section_type == 'problem':
        assignment = assignment_problem
        if tab_idx == 0:
            t = 'human' if assignment == 'human_first' else 'llm'
        else:
            t = 'llm' if assignment == 'human_first' else 'human'
        return (case['human_problem'] if t == 'human' else case['llm_problem'],
                'A' if tab_idx == 0 else 'B')
    else:
        # 'both' — return both texts
        return None, None


def get_both_texts(case, section_type, assignment_summary, assignment_problem):
    """Return (text_a, text_b) for side-by-side or both-type steps."""
    if section_type == 'summary':
        assignment = assignment_summary
        if assignment == 'human_first':
            return case['human_summary'], case['llm_summary']
        else:
            return case['llm_summary'], case['human_summary']
    elif section_type == 'problem':
        assignment = assignment_problem
        if assignment == 'human_first':
            return case['human_problem'], case['llm_problem']
        else:
            return case['llm_problem'], case['human_problem']
    else:
        return case['human_summary'], case['llm_summary']  # fallback


# ── auth ──────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def _get_user_role(username):
    users = load_json(USERS_FILE, {})
    return (users.get(username) or {}).get('role') or 'evaluator'


def annotator_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        if _get_user_role(session['username']) != 'annotator':
            return redirect(url_for('evaluate_resume'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        if _get_user_role(session['username']) != 'admin':
            return ('Forbidden', 403)
        return f(*args, **kwargs)
    return decorated


def _counterbalanced_assignments(case_ids):
    """Generate counterbalanced version assignments.

    Matches the blinding logic in main_streamlit.py:
    - Exactly half of cases get 'human_first' (human=Version A),
      the other half get 'llm_first' (llm=Version A).
    - The split is randomised independently for summary and problem.
    """
    n = len(case_ids)
    num_human_first = n // 2
    num_llm_first = n - num_human_first

    labels_summary = ['human_first'] * num_human_first + ['llm_first'] * num_llm_first
    random.shuffle(labels_summary)

    labels_problem = ['human_first'] * num_human_first + ['llm_first'] * num_llm_first
    random.shuffle(labels_problem)

    assign_summary = {cid: lab for cid, lab in zip(case_ids, labels_summary)}
    assign_problem = {cid: lab for cid, lab in zip(case_ids, labels_problem)}
    return assign_summary, assign_problem


def init_evaluator(username, texts):
    """Create evaluator entry if not present, return user data dict.

    Resets saved data if case_order doesn't match current texts (e.g. after
    switching from legacy to external cases).
    """
    current_ids = {str(t['id']) for t in texts}
    user_data = load_responses(username)

    # Reset if saved case_order references stale/unknown case IDs
    if user_data:
        saved_ids = set(user_data.get('case_order', []))
        if saved_ids and not saved_ids.issubset(current_ids):
            user_data = {}  # force re-init

    if not user_data:
        case_order = [str(t['id']) for t in texts]
        random.shuffle(case_order)
        assign_summary, assign_problem = _counterbalanced_assignments(case_order)
        user_data = {
            'consent_given': False,
            'page': 'intro',
            'case_order': case_order,
            'assignments_summary': assign_summary,
            'assignments_problem': assign_problem,
            'ratings': {},
            'final_questions': {},
            'completed': False,
            'started_at': datetime.utcnow().isoformat(),
        }
        save_responses(username, user_data)
    return user_data


# ── step navigation helpers ───────────────────────────────────────────────────

def step_has_tabs(step):
    return step['has_tabs']


def max_tabs_for_step(step):
    """Return number of tabs for a step (2 if has_tabs, else 1)."""
    return 2 if step['has_tabs'] else 1


def is_step_done(user_data, case_id, step_idx):
    """Return True if the given step for the case is fully rated."""
    ratings = user_data.get('ratings', {})
    case_ratings = ratings.get(case_id, {})
    step = RATING_STEPS[step_idx]
    key = step['key']
    if step['has_tabs']:
        return (key + '_tab0' in case_ratings and key + '_tab1' in case_ratings)
    return key in case_ratings


def compute_case_progress(user_data, case_id):
    """Return (done_steps, total_steps) for a given case."""
    total = len(RATING_STEPS)
    done  = sum(1 for i in range(total) if is_step_done(user_data, case_id, i))
    return done, total


def find_resume_point(user_data):
    """Return (case_id, step_idx, tab_idx) for where to resume."""
    case_order = user_data.get('case_order', [])
    for case_id in case_order:
        for step_idx, step in enumerate(RATING_STEPS):
            key = step['key']
            case_ratings = user_data.get('ratings', {}).get(case_id, {})
            if step['has_tabs']:
                if key + '_tab0' not in case_ratings:
                    return case_id, step_idx, 0
                if key + '_tab1' not in case_ratings:
                    return case_id, step_idx, 1
            else:
                if key not in case_ratings:
                    return case_id, step_idx, 0
    return None, None, None  # all done


# ── routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'username' not in session:
        return redirect(url_for('login'))
    if _get_user_role(session['username']) == 'annotator':
        return redirect(url_for('annotate_index'))
    return redirect(url_for('evaluate_resume'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        users = load_json(USERS_FILE, {})
        if username in users and check_password_hash(users[username]['password'], password):
            session['username'] = username
            if (users[username].get('role') or 'evaluator') == 'annotator':
                return redirect(url_for('annotate_index'))
            return redirect(url_for('evaluate_resume'))
        error = 'Benutzername oder Passwort ungültig.'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/intro', methods=['GET', 'POST'])
@login_required
def intro():
    username = session['username']
    texts    = load_texts()
    user_data = init_evaluator(username, texts)
    has_responses = bool(user_data.get('ratings'))
    if request.method == 'POST':
        user_data = load_responses(username)
        user_data['page'] = 'consent'
        save_responses(username, user_data)
        return redirect(url_for('consent'))
    return render_template('intro.html', has_responses=has_responses)


@app.route('/consent', methods=['GET', 'POST'])
@login_required
def consent():
    username = session['username']
    if request.method == 'POST':
        agreed = request.form.get('agree') == 'on'
        if agreed:
            user_data = load_responses(username)
            already_started = user_data.get('consent_given', False)
            user_data['consent_given'] = True
            user_data['page'] = 'demographics'
            if not already_started:
                user_data['started_at'] = datetime.utcnow().isoformat()
            save_responses(username, user_data)
            if not already_started:
                _send_notification_email(
                    subject=f'[tumor_board_eval] Studie gestartet: {username}',
                    body=(
                        f'Benutzer:    {username}\n'
                        f'Zeitpunkt:   {user_data["started_at"]} UTC\n'
                        f'Ereignis:    Einwilligung erteilt, Studie gestartet.\n'
                    ),
                )
            return redirect(url_for('demographics'))
        return render_template('consent.html', error='Bitte stimmen Sie der Teilnahme zu, um fortzufahren.')
    return render_template('consent.html', error=None)


@app.route('/demographics', methods=['GET', 'POST'])
@login_required
def demographics():
    username = session['username']
    user_data = load_responses(username)

    if not user_data.get('consent_given'):
        return redirect(url_for('consent'))

    existing = user_data.get('demographics', {})

    if request.method == 'POST':
        experience_years = request.form.get('experience_years', '').strip()
        dermatology_years = request.form.get('dermatology_years', '').strip()
        role = request.form.get('role', '').strip()
        role_other = request.form.get('role_other', '').strip()

        if not experience_years or not dermatology_years or not role:
            return render_template('demographics.html',
                error='Bitte füllen Sie alle Pflichtfelder aus.',
                existing=request.form)

        try:
            exp_int = int(experience_years)
            derm_int = int(dermatology_years)
            if not (0 <= exp_int <= 60):
                raise ValueError('Berufserfahrung muss zwischen 0 und 60 liegen.')
            if not (0 <= derm_int <= 60):
                raise ValueError('Dermatologie-Erfahrung muss zwischen 0 und 60 liegen.')
        except ValueError as e:
            return render_template('demographics.html',
                error=str(e), existing=request.form)

        if role == 'other' and not role_other:
            return render_template('demographics.html',
                error='Bitte geben Sie Ihre Funktion an.',
                existing=request.form)

        demo_data = {
            'experience_years': exp_int,
            'dermatology_years': derm_int,
            'role': role,
            'saved_at': datetime.utcnow().isoformat(),
        }
        if role == 'other':
            demo_data['role_other'] = role_other

        user_data = load_responses(username)
        user_data['demographics'] = demo_data
        user_data['page'] = 'study_info'
        save_responses(username, user_data)
        return redirect(url_for('study_info'))

    return render_template('demographics.html', error=None, existing=existing)


@app.route('/study_info', methods=['GET', 'POST'])
@login_required
def study_info():
    username = session['username']
    user_data = load_responses(username)

    if not user_data.get('consent_given'):
        return redirect(url_for('consent'))
    if not user_data.get('demographics'):
        return redirect(url_for('demographics'))

    if request.method == 'POST':
        user_data = load_responses(username)
        user_data['study_info_seen'] = True
        user_data['page'] = 'evaluation'
        save_responses(username, user_data)
        return redirect(url_for('evaluate_resume'))

    texts = load_texts()
    return render_template('study_info.html', num_cases=len(texts))


@app.route('/evaluate')
@login_required
def evaluate_resume():
    username = session['username']
    texts    = load_texts()
    if not texts:
        return render_template('error.html',
            message='Keine Texte gefunden. Bitte texts.json befüllen.')

    user_data = init_evaluator(username, texts)

    # Redirect to intro/consent/demographics if not done yet
    if not user_data.get('consent_given'):
        page = user_data.get('page', 'intro')
        if page == 'intro':
            return redirect(url_for('intro'))
        return redirect(url_for('consent'))

    if not user_data.get('demographics'):
        return redirect(url_for('demographics'))

    if not user_data.get('study_info_seen'):
        return redirect(url_for('study_info'))

    if user_data.get('completed'):
        return redirect(url_for('end'))

    # If final_questions not yet done but all cases rated
    case_order = user_data.get('case_order', [])
    all_cases_done = all(
        all(is_step_done(user_data, cid, i) for i in range(len(RATING_STEPS)))
        for cid in case_order
    )
    if all_cases_done and not user_data.get('final_questions'):
        return redirect(url_for('final_questions'))

    case_id, step_idx, tab_idx = find_resume_point(user_data)
    if case_id is None:
        return redirect(url_for('final_questions'))

    # Show patient dashboard before first step of each case
    if step_idx == 0 and tab_idx == 0:
        dashboards_seen = user_data.get('dashboards_seen', [])
        if case_id not in dashboards_seen:
            return redirect(url_for('case_dashboard', case_id=case_id))

    return redirect(url_for('evaluate_step',
                            case_id=case_id,
                            step_idx=step_idx,
                            tab_idx=tab_idx))


@app.route('/evaluate/<case_id>/step/<int:step_idx>/tab/<int:tab_idx>',
           methods=['GET', 'POST'])
@login_required
def evaluate_step(case_id, step_idx, tab_idx):
    username = session['username']
    texts    = load_texts()
    text_dict = {str(t['id']): t for t in texts}

    if case_id not in text_dict:
        return redirect(url_for('evaluate_resume'))
    if step_idx < 0 or step_idx >= len(RATING_STEPS):
        return redirect(url_for('evaluate_resume'))

    responses = init_evaluator(username, texts)
    user_data = responses

    if not user_data.get('consent_given'):
        return redirect(url_for('consent'))

    case         = text_dict[case_id]
    step         = RATING_STEPS[step_idx]
    case_order   = user_data.get('case_order', [])
    case_index   = case_order.index(case_id) if case_id in case_order else 0

    assign_sum  = user_data['assignments_summary'].get(case_id, 'human_first')
    assign_prob = user_data['assignments_problem'].get(case_id, 'human_first')

    # For tab steps, tab_idx must be 0 or 1
    if step['has_tabs']:
        if tab_idx not in (0, 1):
            return redirect(url_for('evaluate_step',
                                    case_id=case_id, step_idx=step_idx, tab_idx=0))
    else:
        tab_idx = 0

    # Get text(s) for this step
    if not step['has_tabs']:
        text_a, text_b = get_both_texts(case, step['section_type'], assign_sum, assign_prob)
        text_content   = None
    else:
        text_content, _ = get_text_for_tab(case, step['section_type'],
                                            assign_sum, assign_prob, tab_idx)
        text_a = text_b = None

    # Admin: determine origin (human/llm) of the currently shown text(s)
    is_admin = _get_user_role(username) == 'admin'
    text_origin = text_a_origin = text_b_origin = None
    text_content_raw = text_a_raw = text_b_raw = None
    if is_admin and step['section_type'] in ('summary', 'problem'):
        assignment = assign_sum if step['section_type'] == 'summary' else assign_prob
        if step['has_tabs']:
            if tab_idx == 0:
                text_origin = 'human' if assignment == 'human_first' else 'llm'
            else:
                text_origin = 'llm' if assignment == 'human_first' else 'human'
            text_content_raw = text_content or ''
        else:
            if assignment == 'human_first':
                text_a_origin, text_b_origin = 'human', 'llm'
            else:
                text_a_origin, text_b_origin = 'llm', 'human'
            text_a_raw, text_b_raw = text_a or '', text_b or ''

    # Existing rating
    case_ratings = user_data.get('ratings', {}).get(case_id, {})
    key          = step['key']
    storage_key  = (key + '_tab' + str(tab_idx)) if step['has_tabs'] else key
    existing     = case_ratings.get(storage_key, {})

    # Count done cases for progress
    done_cases = sum(
        1 for cid in case_order
        if all(is_step_done(user_data, cid, i) for i in range(len(RATING_STEPS)))
    )
    total_cases = len(case_order)

    # Step statuses for sidebar
    step_statuses = []
    for i, s in enumerate(RATING_STEPS):
        if i == step_idx:
            st = 'process'
        elif is_step_done(user_data, case_id, i):
            st = 'finish'
        else:
            st = 'wait'
        step_statuses.append(st)

    # Derive item lists for consequences-only steps from researcher annotations.
    # hl_version_key tells us which text version (human_summary / llm_summary)
    # is currently shown in this tab; we look up the matching annotation slot.
    enthalten_items = []  # legacy kwarg name, now holds the falsch items to rate
    hl_version_key = None
    if key in ('summary_falseinfo', 'summary_missinginfo', 'summary_correctness', 'summary_completeness', 'summary_conciseness'):
        assign = assign_sum
        if tab_idx == 0:
            hl_version_key = 'human_summary' if assign == 'human_first' else 'llm_summary'
        else:
            hl_version_key = 'llm_summary' if assign == 'human_first' else 'human_summary'

    def _annotation_items_for(case_id_, text_key, target_status):
        try:
            ann = _load_annotation(case_id_) or {}
        except Exception:
            return set()
        items = ((ann.get('texts') or {}).get(text_key) or {}).get('items') or {}
        return {slug for slug, entry in items.items()
                if (entry or {}).get('status') == target_status}

    if key == 'summary_falseinfo' and hl_version_key:
        try:
            ann_for_order = _load_annotation(case_id) or {}
        except Exception:
            ann_for_order = {}
        ann_items_for_order = ((ann_for_order.get('texts') or {}).get(hl_version_key) or {}).get('items') or {}
        falsch_slugs = {slug for slug, entry in ann_items_for_order.items()
                        if (entry or {}).get('status') == 'falsch'}

        def _first_span_pos(slug):
            entry = ann_items_for_order.get(slug) or {}
            positions = []
            for span in (entry.get('spans') or []):
                if span and text_content:
                    idx = text_content.find(span)
                    if idx >= 0:
                        positions.append(idx)
            return min(positions) if positions else float('inf')

        falsch_items_in_text_order = sorted(
            (item for item in INFO_ITEMS if slugify(item) in falsch_slugs),
            key=lambda it: (_first_span_pos(slugify(it)), INFO_ITEMS.index(it)),
        )
        enthalten_items.extend(falsch_items_in_text_order)

    # Compute "false" items for the correctness step (display-only list).
    false_items = []
    if key == 'summary_correctness' and hl_version_key:
        try:
            ann_fc = _load_annotation(case_id) or {}
        except Exception:
            ann_fc = {}
        ann_items_fc = ((ann_fc.get('texts') or {}).get(hl_version_key) or {}).get('items') or {}
        falsch_slugs_fc = {slug for slug, entry in ann_items_fc.items()
                           if (entry or {}).get('status') == 'falsch'}
        for item in INFO_ITEMS:
            if slugify(item) in falsch_slugs_fc:
                false_items.append(item)

    # Compute "missing" items: annotated as nicht_enthalten AND marked relevant
    missing_items = []
    if key in ('summary_missinginfo', 'summary_completeness') and hl_version_key:
        relevance = case_ratings.get('case_relevance', {})
        ne_slugs = _annotation_items_for(case_id, hl_version_key, 'nicht_enthalten')
        for item in INFO_ITEMS:
            slug = slugify(item)
            if slug in ne_slugs and relevance.get('relevant_' + slug) == 'yes':
                missing_items.append(item)

    # Compute "irrelevant but present" items for the conciseness step
    # (display-only list).
    irrelevant_items = []
    if key == 'summary_conciseness' and hl_version_key:
        relevance = case_ratings.get('case_relevance', {})
        enthalten_slugs = _annotation_items_for(case_id, hl_version_key, 'enthalten')
        for item in INFO_ITEMS:
            slug = slugify(item)
            if slug in enthalten_slugs and relevance.get('relevant_' + slug) == 'no':
                irrelevant_items.append(item)

    # Check if all prior steps are done (for final_overall gate)
    all_prior_done = all(
        is_step_done(user_data, case_id, i)
        for i in range(len(RATING_STEPS))
        if RATING_STEPS[i]['key'] != 'final_overall'
    )

    # Which steps are still missing (for tooltip on disabled button)
    missing_step_names = []
    if not all_prior_done:
        for i, s in enumerate(RATING_STEPS):
            if s['key'] != 'final_overall' and not is_step_done(user_data, case_id, i):
                missing_step_names.append(s['subtitle'])

    error = None

    if request.method == 'POST':
        # Server-side guard: cannot submit final_overall unless all others done
        if key == 'final_overall' and not all_prior_done:
            error = 'Bitte schlie\u00dfen Sie zuerst alle vorherigen Bewertungsschritte ab.'
        else:
            rating, error = parse_step_form(step, tab_idx, request.form, enthalten_items, missing_items)
        if error is None:
            rating['saved_at'] = datetime.utcnow().isoformat()
            user_data = load_responses(username)
            user_data['ratings'].setdefault(case_id, {})[storage_key] = rating
            save_responses(username, user_data)

            # Notify when the last step of a case is submitted
            if key == 'final_overall':
                completed_count = sum(
                    1 for cid in case_order
                    if all(is_step_done(user_data, cid, i) for i in range(len(RATING_STEPS)))
                )
                _send_notification_email(
                    subject=f'[tumor_board_eval] Fall abgeschlossen: {username}',
                    body=(
                        f'Benutzer:           {username}\n'
                        f'Fall-ID:            {case_id}\n'
                        f'Gesamt abgeschlossen: {completed_count} / {len(case_order)}\n'
                        f'Zeitpunkt:          {rating["saved_at"]} UTC\n'
                    ),
                )

            # Navigate to next step/tab
            next_url = compute_next_url(user_data, case_id, case_order, case_index,
                                        step_idx, tab_idx, step)
            return redirect(next_url)
        else:
            # Validation failed – merge submitted form values into existing
            # so the template preserves what the user already rated.
            for form_key in request.form:
                existing[form_key] = request.form[form_key]

    # Previous URL
    prev_url = compute_prev_url(case_id, case_order, case_index, step_idx, tab_idx, step)

    # Apply highlight annotations for falseinfo / integrity steps
    protocol_excerpts = {}
    ground_truth_excerpts = {}
    if key in ('summary_falseinfo', 'summary_correctness') and text_content and hl_version_key:
        # Highlight the annotator-flagged falsch spans in red for both steps.
        try:
            ann = _load_annotation(case_id) or {}
        except Exception:
            ann = {}
        ann_items = ((ann.get('texts') or {}).get(hl_version_key) or {}).get('items') or {}
        falsch_mapping = {
            slug: [s for s in (entry.get('spans') or []) if s]
            for slug, entry in ann_items.items()
            if (entry or {}).get('status') == 'falsch'
        }
        falsch_mapping = {k: v for k, v in falsch_mapping.items() if v}
        rendered_text = _annotate_highlights(text_content, falsch_mapping,
                                             extra_class='hl-info--error')
        hl_mappings = _load_highlight_mappings()
        protocol_excerpts = hl_mappings.get(case_id, {}).get('protocol', {})
        ground_truth_excerpts = _build_ground_truth_excerpts(case_id, protocol_excerpts)
    else:
        rendered_text = _bold_headers(text_content)

    return render_template('evaluate.html',
        case_id=case_id,
        case_index=case_index,
        total_cases=total_cases,
        done_cases=done_cases,
        step=step,
        step_idx=step_idx,
        tab_idx=tab_idx,
        text_content=rendered_text,
        text_a=_bold_headers(text_a),
        text_b=_bold_headers(text_b),
        existing=existing,
        case_ratings=case_ratings,
        step_statuses=step_statuses,
        error=error,
        prev_url=prev_url,
        info_items=INFO_ITEMS,
        enthalten_items=enthalten_items,
        missing_items=missing_items,
        false_items=false_items,
        irrelevant_items=irrelevant_items,
        protocol_excerpts=protocol_excerpts,
        ground_truth_excerpts=ground_truth_excerpts,
        slugify=slugify,
        rating_steps=RATING_STEPS,
        all_prior_done=all_prior_done,
        missing_step_names=missing_step_names,
        is_admin=is_admin,
        text_origin=text_origin,
        text_a_origin=text_a_origin,
        text_b_origin=text_b_origin,
        text_content_raw=text_content_raw,
        text_a_raw=text_a_raw,
        text_b_raw=text_b_raw,
    )


def parse_step_form(step, tab_idx, form, enthalten_items, missing_items=None):
    """Parse form data for the given step. Returns (rating_dict, error_string)."""
    key = step['key']
    rating = {}

    try:
        if key == 'case_relevance':
            missing = []
            for item in INFO_ITEMS:
                slug = slugify(item)
                val = form.get('relevant_' + slug, '')
                rating['relevant_' + slug] = val
                if val not in ('yes', 'no'):
                    missing.append(item)
            if missing:
                return None, f'Bitte bewerten Sie alle Informationen. Es fehlen noch {len(missing)} Bewertung(en).'
            rating['comment'] = form.get('comment', '').strip()

        elif key == 'summary_correctness':
            vas = form.get('vas_score', '').strip()
            if not vas:
                return None, 'Bitte bewerten Sie die Korrektheit auf der Skala.'
            try:
                rating['vas_score'] = int(vas)
            except ValueError:
                return None, 'Ungültiger Wert für die Bewertung.'
            if not (0 <= rating['vas_score'] <= 100):
                return None, 'Bewertung muss zwischen 0 und 100 liegen.'
            rating['comment'] = form.get('comment', '').strip()
            for item in INFO_ITEMS:
                slug = slugify(item)
                rating['relevant_' + slug] = form.get('relevant_' + slug, '')

        elif key in ('summary_completeness', 'summary_conciseness'):
            vas = form.get('vas_score', '').strip()
            if not vas:
                return None, 'Bitte bewerten Sie auf der Skala.'
            try:
                rating['vas_score'] = int(vas)
            except ValueError:
                return None, 'Ungültiger Wert für die Bewertung.'
            if not (0 <= rating['vas_score'] <= 100):
                return None, 'Bewertung muss zwischen 0 und 100 liegen.'
            rating['comment'] = form.get('comment', '').strip()
            for item in INFO_ITEMS:
                slug = slugify(item)
                rating['relevant_' + slug] = form.get('relevant_' + slug, '')

        elif key in ('summary_postedit', 'problem_postedit'):
            pe_decision = form.get('pe_decision', '').strip()
            if not pe_decision:
                return None, 'Bitte wählen Sie eine Option aus.'
            rating['pe_decision'] = pe_decision
            if pe_decision == 'needs_edit' and key == 'summary_postedit':
                pe_effort = form.get('pe_effort', '').strip()
                if pe_effort:
                    rating['pe_effort'] = pe_effort
            rating['comment'] = form.get('comment', '').strip()

        elif key == 'summary_falseinfo':
            # Item list is fixed by the researcher annotation (passed in as
            # `enthalten_items` for legacy kwarg compatibility). All listed
            # items must be rated for severity + probability.
            false_items = list(enthalten_items or [])
            rating['false_items'] = false_items
            incomplete = []
            for item in false_items:
                slug = slugify(item)
                severity = form.get('severity_' + slug, '')
                prob     = form.get('prob_' + slug, '')
                rating['severity_' + slug] = severity
                rating['prob_' + slug]     = prob
                rating['comment_' + slug]  = form.get('comment_' + slug, '').strip()
                if not severity or not prob:
                    incomplete.append(item)
            if incomplete:
                return None, f'Bitte bewerten Sie alle Informationen. Es fehlen noch {len(incomplete)} Bewertung(en).'
            rating['comment'] = form.get('comment', '').strip()

        elif key == 'summary_missinginfo':
            if missing_items is None:
                missing_items = []
            # All missing_items are implicitly flagged; validate each has severity + prob
            rating['missing_items'] = missing_items
            incomplete = []
            for item in missing_items:
                slug = slugify(item)
                severity = form.get('severity_' + slug, '')
                prob     = form.get('prob_' + slug, '')
                rating['severity_' + slug] = severity
                rating['prob_' + slug]     = prob
                rating['comment_' + slug]  = form.get('comment_' + slug, '').strip()
                if not severity or not prob:
                    incomplete.append(item)
            if incomplete:
                return None, f'Bitte bewerten Sie alle Informationen. Es fehlen noch {len(incomplete)} Bewertung(en).'
            rating['comment'] = form.get('comment', '').strip()

        elif key in ('summary_origin_guess', 'problem_origin_guess'):
            val = form.get('choice')
            if not val:
                return None, 'Bitte wählen Sie eine Option aus.'
            rating['choice'] = val
            rating['comment'] = form.get('comment', '').strip()

        elif key in ('summary_preference', 'problem_preference'):
            val = form.get('choice')
            if not val:
                return None, 'Bitte wählen Sie eine Präferenz aus.'
            rating['choice'] = val
            rating['comment'] = form.get('comment', '').strip()

        elif key == 'problem_focus':
            topics = form.getlist('topics')
            diagnostik_types = form.getlist('diagnostik_types')
            therapie_types = form.getlist('therapie_types')
            if 'Weitere Diagnostik' in topics and not diagnostik_types:
                return None, 'Bitte spezifizieren Sie die Art der Diagnostik.'
            if 'Therapie (Beginn, Auswahl, Modifikation)' in topics and not therapie_types:
                return None, 'Bitte spezifizieren Sie die vorgeschlagene Therapie.'
            rating['topics'] = topics
            rating['diagnostik_types'] = diagnostik_types
            rating['therapie_types'] = therapie_types
            rating['comment'] = form.get('comment', '').strip()

        elif key in ('problem_correctness', 'problem_specificity'):
            val = form.get('choice')
            if not val:
                return None, 'Bitte wählen Sie eine Option aus.'
            rating['choice'] = val
            rating['comment'] = form.get('comment', '').strip()

        elif key == 'final_overall':
            vas = form.get('vas_score', '').strip()
            if not vas:
                return None, 'Bitte bewerten Sie die Fallkomplexität auf der Skala.'
            rating['vas_score'] = int(vas)
            if not (0 <= rating['vas_score'] <= 100):
                return None, 'Bewertung muss zwischen 0 und 100 liegen.'
            rating['comment'] = form.get('comment', '').strip()

    except (ValueError, TypeError) as e:
        return None, f'Ungültige Eingabe: {e}'

    return rating, None


def compute_next_url(user_data, case_id, case_order, case_index,
                     step_idx, tab_idx, step):
    """Return the URL to redirect to after submitting a step."""
    # Reload fresh data
    username = session.get('username', '')
    user_data = load_responses(username) if username else user_data

    if step['has_tabs'] and tab_idx == 0:
        # Go to tab 1 of same step
        return url_for('evaluate_step',
                       case_id=case_id, step_idx=step_idx, tab_idx=1)

    next_step = step_idx + 1
    if next_step < len(RATING_STEPS):
        return url_for('evaluate_step',
                       case_id=case_id, step_idx=next_step, tab_idx=0)

    # All steps for this case done — next case (show dashboard first) or final questions
    if case_index + 1 < len(case_order):
        next_case = case_order[case_index + 1]
        return url_for('case_dashboard', case_id=next_case)

    return url_for('final_questions')


def compute_prev_url(case_id, case_order, case_index, step_idx, tab_idx, step):
    """Return the URL for the back button."""
    if step['has_tabs'] and tab_idx == 1:
        return url_for('evaluate_step',
                       case_id=case_id, step_idx=step_idx, tab_idx=0)
    if step_idx > 0:
        prev_step = RATING_STEPS[step_idx - 1]
        prev_tab  = 1 if prev_step['has_tabs'] else 0
        return url_for('evaluate_step',
                       case_id=case_id, step_idx=step_idx - 1, tab_idx=prev_tab)
    # step_idx == 0, tab 0 → back to this case's dashboard
    return url_for('case_dashboard', case_id=case_id)


@app.route('/final-questions', methods=['GET', 'POST'])
@login_required
def final_questions():
    username = session['username']
    user_data = load_responses(username)

    if request.method == 'POST':
        fq = {
            'blinding_seen_through': request.form.get('blinding_seen_through', ''),
            'blinding_indicators':   request.form.get('blinding_indicators', '').strip(),
            'additional_comments':   request.form.get('additional_comments', '').strip(),
            'saved_at':              datetime.utcnow().isoformat(),
        }
        user_data = load_responses(username)
        was_completed = user_data.get('completed', False)
        user_data['final_questions'] = fq
        user_data['completed']       = True
        user_data['completed_at']    = datetime.utcnow().isoformat()
        save_responses(username, user_data)
        _export_all_to_disk(username)
        if not was_completed:
            started_at = user_data.get('started_at', 'unbekannt')
            num_cases  = len(user_data.get('responses', {}) or {})
            _send_notification_email(
                subject=f'[tumor_board_eval] Studie abgeschlossen: {username}',
                body=(
                    f'Benutzer:        {username}\n'
                    f'Gestartet:       {started_at} UTC\n'
                    f'Abgeschlossen:   {user_data["completed_at"]} UTC\n'
                    f'Bewertete Fälle: {num_cases}\n'
                    f'Ereignis:        Alle Fälle und Abschlussfragen abgeschlossen.\n'
                ),
            )
        return redirect(url_for('end'))

    existing_fq = user_data.get('final_questions', {})
    return render_template('final_questions.html', existing=existing_fq)


@app.route('/end')
@login_required
def end():
    username  = session['username']
    user_data = load_responses(username)
    texts     = load_texts()
    total_cases = len(texts)
    done_cases  = sum(
        1 for cid in user_data.get('case_order', [])
        if all(is_step_done(user_data, cid, i) for i in range(len(RATING_STEPS)))
    )
    return render_template('end.html',
                           username=username,
                           done_cases=done_cases,
                           total_cases=total_cases)


@app.route('/guideline-pdf')
@login_required
def guideline_pdf():
    """Serve the S3-Leitlinie guideline PDF."""
    if os.path.isdir(GUIDELINE_DIR):
        for fname in sorted(os.listdir(GUIDELINE_DIR)):
            if fname.lower().endswith('.pdf'):
                resp = send_file(os.path.join(GUIDELINE_DIR, fname),
                                 mimetype='application/pdf')
                resp.headers['Content-Disposition'] = 'inline'
                return resp
    return 'Keine Leitlinie gefunden.', 404


@app.route('/admin/text/<case_id>', methods=['POST'])
@admin_required
def admin_save_text(case_id):
    """Admin: overwrite a case's source text file (texts_human / texts_llm)."""
    if not _is_case_id(case_id):
        # Allow any case_id stem actually present in cases
        if case_id not in _discover_cases():
            return jsonify({'error': 'unknown case_id'}), 404
    payload = request.get_json(silent=True) or {}
    section = payload.get('section')
    origin  = payload.get('origin')
    text    = payload.get('text')
    if section not in ('summary', 'problem') or origin not in ('human', 'llm') or text is None:
        return jsonify({'error': 'invalid payload'}), 400
    sub = 'zusammenfassung' if section == 'summary' else 'fragestellung'
    base = TEXTS_HUMAN_DIR if origin == 'human' else TEXTS_LLM_DIR
    target_dir = os.path.join(base, sub)
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, f'{case_id}.txt')
    with open(target, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text.rstrip() + '\n')
    return jsonify({'ok': True})


def _resolve_version(step, section_type, assign_sum, assign_prob, case_id, tab_idx):
    """Return the actual version ('human' | 'llm' | 'both') for a given tab."""
    if not step['has_tabs']:
        return 'both'
    a = assign_sum.get(case_id, 'human_first') if section_type == 'summary' \
        else assign_prob.get(case_id, 'human_first') if section_type == 'problem' \
        else assign_sum.get(case_id, 'human_first')
    if (a == 'human_first' and tab_idx == 0) or (a == 'llm_first' and tab_idx == 1):
        return 'human'
    return 'llm'


# ---------------------------------------------------------------------------
#  ratings.csv  –  one row per evaluator × case × step × version
# ---------------------------------------------------------------------------

def _duration_seconds(started_at, completed_at):
    """Return integer seconds between two ISO timestamps, or None."""
    if not started_at or not completed_at:
        return None
    try:
        s = datetime.fromisoformat(started_at)
        c = datetime.fromisoformat(completed_at)
        return int((c - s).total_seconds())
    except (ValueError, TypeError):
        return None


def _generate_ratings_csv():
    """Long-format ratings CSV optimised for IRR and mixed-effects models."""
    responses = load_all_responses()

    output = io.StringIO()
    writer = csv.writer(output)

    header = [
        'evaluator', 'case_id', 'case_order_position',
        'section_type', 'step_key', 'version', 'presentation_order',
        'vas_score', 'choice', 'pe_decision',
        'behandlungsziel', 'problem_status', 'problem_topics',
        'diagnostik_types', 'therapie_types',
        'integrity_enthalten_count', 'falseinfo_count', 'missinginfo_count',
        'comment', 'saved_at',
        'assignment_summary', 'assignment_problem',
        'experience_years', 'dermatology_years', 'role', 'role_other',
        'completed', 'started_at', 'completed_at', 'duration_seconds',
        'final_blinding_seen_through', 'final_blinding_indicators',
        'final_additional_comments',
    ]
    writer.writerow(header)

    for username, data in sorted(responses.items()):
        assign_sum  = data.get('assignments_summary', {})
        assign_prob = data.get('assignments_problem', {})
        ratings     = data.get('ratings', {})
        case_order  = data.get('case_order', [])
        demo        = data.get('demographics', {})
        fq          = data.get('final_questions', {}) or {}
        completed   = data.get('completed', False)
        started_at  = data.get('started_at', '')
        completed_at = data.get('completed_at', '')
        duration_s  = _duration_seconds(started_at, completed_at)

        case_pos = {cid: pos for pos, cid in enumerate(case_order)}

        for case_id in sorted(ratings.keys(), key=lambda x: int(x) if x.isdigit() else x):
            case_ratings = ratings[case_id]

            for step in RATING_STEPS:
                key = step['key']
                section_type = step['section_type']

                if step['has_tabs']:
                    tabs = [('_tab0', 0), ('_tab1', 1)]
                else:
                    tabs = [('', 0)]

                for suffix, t_idx in tabs:
                    r = case_ratings.get(key + suffix)
                    if r is None:
                        continue

                    version = _resolve_version(
                        step, section_type, assign_sum, assign_prob,
                        case_id, t_idx)

                    # Counts
                    enthalten_count = ''
                    if key == 'summary_integrity':
                        enthalten_count = sum(
                            1 for item in INFO_ITEMS
                            if r.get('enthalten_' + slugify(item)) == 'yes')

                    false_count = ''
                    if key == 'summary_falseinfo':
                        false_count = len(r.get('false_items', []))

                    missing_count = ''
                    if key == 'summary_missinginfo':
                        missing_count = len(r.get('missing_items', []))

                    writer.writerow([
                        username,
                        case_id,
                        case_pos.get(case_id, ''),
                        section_type,
                        key,
                        version,
                        t_idx if step['has_tabs'] else '',
                        r.get('vas_score', ''),
                        r.get('choice', ''),
                        r.get('pe_decision', ''),
                        r.get('behandlungsziel', ''),
                        r.get('status', ''),
                        '; '.join(r.get('topics', [])) if key == 'problem_focus' else '',
                        '; '.join(r.get('diagnostik_types', [])) if key == 'problem_focus' else '',
                        '; '.join(r.get('therapie_types', [])) if key == 'problem_focus' else '',
                        enthalten_count,
                        false_count,
                        missing_count,
                        r.get('comment', ''),
                        r.get('saved_at', ''),
                        assign_sum.get(case_id, ''),
                        assign_prob.get(case_id, ''),
                        demo.get('experience_years', ''),
                        demo.get('dermatology_years', ''),
                        demo.get('role', ''),
                        demo.get('role_other', ''),
                        completed,
                        started_at,
                        completed_at,
                        duration_s if duration_s is not None else '',
                        fq.get('blinding_seen_through', ''),
                        fq.get('blinding_indicators', ''),
                        fq.get('additional_comments', ''),
                    ])

    output.seek(0)
    return output.getvalue()


# ---------------------------------------------------------------------------
#  integrity_items.csv  –  one row per evaluator × case × version × item
# ---------------------------------------------------------------------------

def _generate_items_csv():
    """Item-level detail for integrity, relevance, and false-info checklist items."""
    responses = load_all_responses()

    output = io.StringIO()
    writer = csv.writer(output)

    header = [
        'evaluator', 'case_id', 'version',
        'item_category', 'item_name', 'item_slug',
        'is_relevant', 'case_relevance_comment',
        'is_present', 'is_false',
        'false_severity', 'false_probability', 'false_comment',
        'is_missing_flagged',
        'missing_severity', 'missing_probability', 'missing_comment',
    ]
    writer.writerow(header)

    for username, data in sorted(responses.items()):
        assign_sum  = data.get('assignments_summary', {})
        ratings     = data.get('ratings', {})

        for case_id in sorted(ratings.keys(), key=lambda x: int(x) if x.isdigit() else x):
            cr = ratings[case_id]

            # case_relevance (version-independent)
            rel = cr.get('case_relevance', {})
            rel_comment = rel.get('comment', '')

            for item in INFO_ITEMS:
                slug = slugify(item)
                parts = item.split(': ', 1)
                cat   = parts[0] if len(parts) == 2 else ''
                name  = parts[1] if len(parts) == 2 else item
                is_relevant = rel.get('relevant_' + slug, '')

                # Each tab (version) for integrity + falseinfo + missinginfo
                for suffix, t_idx in [('_tab0', 0), ('_tab1', 1)]:
                    a = assign_sum.get(case_id, 'human_first')
                    if (a == 'human_first' and t_idx == 0) or (a == 'llm_first' and t_idx == 1):
                        version = 'human'
                    else:
                        version = 'llm'

                    integrity   = cr.get('summary_integrity' + suffix, {})
                    falseinfo   = cr.get('summary_falseinfo' + suffix, {})
                    missinginfo = cr.get('summary_missinginfo' + suffix, {})

                    is_present = integrity.get('enthalten_' + slug, '')
                    is_false   = 'yes' if item in falseinfo.get('false_items', []) else ''
                    false_sev  = falseinfo.get('severity_' + slug, '')
                    false_prob = falseinfo.get('prob_' + slug, '')
                    false_cmt  = falseinfo.get('comment_' + slug, '')

                    is_missing_flagged = 'yes' if item in missinginfo.get('missing_items', []) else ''
                    missing_sev  = missinginfo.get('severity_' + slug, '')
                    missing_prob = missinginfo.get('prob_' + slug, '')
                    missing_cmt  = missinginfo.get('comment_' + slug, '')

                    # Only write rows where at least one value is populated
                    if any([is_relevant, is_present, is_false, is_missing_flagged]):
                        writer.writerow([
                            username, case_id, version,
                            cat, name, slug,
                            is_relevant, rel_comment,
                            is_present, is_false,
                            false_sev, false_prob, false_cmt,
                            is_missing_flagged,
                            missing_sev, missing_prob, missing_cmt,
                        ])

    output.seek(0)
    return output.getvalue()


# ---------------------------------------------------------------------------
#  JSON export — flat list of records (one per evaluator × case × step × version)
# ---------------------------------------------------------------------------

def _generate_export_json():
    """Build a flat list-of-dicts JSON matching the ratings CSV structure,
    with the raw text content included for each record."""
    responses = load_all_responses()
    texts     = load_texts()
    text_dict = {str(t['id']): t for t in texts}
    records   = []

    for username, data in sorted(responses.items()):
        assign_sum  = data.get('assignments_summary', {})
        assign_prob = data.get('assignments_problem', {})
        ratings     = data.get('ratings', {})
        case_order  = data.get('case_order', [])
        demo        = data.get('demographics', {})
        fq          = data.get('final_questions', {}) or {}
        completed   = data.get('completed', False)
        started_at  = data.get('started_at', '')
        completed_at = data.get('completed_at', '')
        duration_s  = _duration_seconds(started_at, completed_at)

        case_pos = {cid: pos for pos, cid in enumerate(case_order)}

        for case_id in sorted(ratings.keys(), key=lambda x: int(x) if x.isdigit() else x):
            case_ratings = ratings[case_id]
            case_texts   = text_dict.get(case_id, {})
            case_relevance_comment = (case_ratings.get('case_relevance', {}) or {}).get('comment', '')

            for step in RATING_STEPS:
                key = step['key']
                section_type = step['section_type']

                if step['has_tabs']:
                    tabs = [('_tab0', 0), ('_tab1', 1)]
                else:
                    tabs = [('', 0)]

                for suffix, t_idx in tabs:
                    r = case_ratings.get(key + suffix)
                    if r is None:
                        continue

                    version = _resolve_version(
                        step, section_type, assign_sum, assign_prob,
                        case_id, t_idx)

                    # Resolve raw texts for this record
                    text_summary = None
                    text_problem = None
                    if version == 'human':
                        text_summary = case_texts.get('human_summary')
                        text_problem = case_texts.get('human_problem')
                    elif version == 'llm':
                        text_summary = case_texts.get('llm_summary')
                        text_problem = case_texts.get('llm_problem')
                    else:  # 'both'
                        text_summary = {
                            'human': case_texts.get('human_summary'),
                            'llm':   case_texts.get('llm_summary'),
                        }
                        text_problem = {
                            'human': case_texts.get('human_problem'),
                            'llm':   case_texts.get('llm_problem'),
                        }

                    # Only attach the text type relevant to the section
                    if section_type == 'summary':
                        text_shown = text_summary
                    elif section_type == 'problem':
                        text_shown = text_problem
                    else:  # 'both'
                        text_shown = {
                            'summary': text_summary,
                            'problem': text_problem,
                        }

                    rec = {
                        'evaluator':            username,
                        'case_id':              int(case_id) if case_id.isdigit() else case_id,
                        'case_order_position':  case_pos.get(case_id),
                        'section_type':         section_type,
                        'step_key':             key,
                        'version':              version,
                        'presentation_order':   t_idx if step['has_tabs'] else None,
                        'text_shown':           text_shown,
                        'vas_score':            r.get('vas_score'),
                        'choice':               r.get('choice'),
                        'pe_decision':          r.get('pe_decision'),
                        'behandlungsziel':      r.get('behandlungsziel'),
                        'problem_status':       r.get('status'),
                        'problem_topics':       r.get('topics'),
                        'diagnostik_types':     r.get('diagnostik_types'),
                        'therapie_types':       r.get('therapie_types'),
                        'integrity_enthalten_count': None,
                        'falseinfo_count':      None,
                        'missinginfo_count':    None,
                        'comment':              r.get('comment'),
                        'saved_at':             r.get('saved_at'),
                        'assignment_summary':   assign_sum.get(case_id),
                        'assignment_problem':   assign_prob.get(case_id),
                        'experience_years':     demo.get('experience_years'),
                        'dermatology_years':    demo.get('dermatology_years'),
                        'role':                 demo.get('role'),
                        'role_other':           demo.get('role_other'),
                        'completed':            completed,
                        'started_at':           started_at or None,
                        'completed_at':         completed_at or None,
                        'duration_seconds':     duration_s,
                        'case_relevance_comment':       case_relevance_comment or None,
                        'final_blinding_seen_through':  fq.get('blinding_seen_through') or None,
                        'final_blinding_indicators':    fq.get('blinding_indicators') or None,
                        'final_additional_comments':    fq.get('additional_comments') or None,
                    }

                    if key == 'summary_integrity':
                        rec['integrity_enthalten_count'] = sum(
                            1 for item in INFO_ITEMS
                            if r.get('enthalten_' + slugify(item)) == 'yes')
                    if key == 'summary_falseinfo':
                        rec['falseinfo_count'] = len(r.get('false_items', []))
                    if key == 'summary_missinginfo':
                        rec['missinginfo_count'] = len(r.get('missing_items', []))

                    records.append(rec)

    return records


# ---------------------------------------------------------------------------
#  Disk writers
# ---------------------------------------------------------------------------

def _export_all_to_disk(username):
    """Write all export files to the exports/ subfolder."""
    os.makedirs(EXPORTS_DIR, exist_ok=True)

    # ratings CSV
    ratings_csv = _generate_ratings_csv()
    path = os.path.join(EXPORTS_DIR, f'ratings_{username}.csv')
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        f.write(ratings_csv)

    # integrity items CSV
    items_csv = _generate_items_csv()
    path = os.path.join(EXPORTS_DIR, f'integrity_items_{username}.csv')
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        f.write(items_csv)

    # flat JSON
    records = _generate_export_json()
    path = os.path.join(EXPORTS_DIR, f'ratings_{username}.json')
    save_json(path, records)

    # raw JSON (archival — per-user response file already contains texts)
    user_data = load_responses(username)
    path = os.path.join(EXPORTS_DIR, f'responses_raw_{username}.json')
    save_json(path, user_data)


@app.route('/export')
@login_required
def export():
    username = session['username']
    _export_all_to_disk(username)
    ratings_csv = _generate_ratings_csv()
    return send_file(
        io.BytesIO(ratings_csv.encode('utf-8-sig')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'ratings_{username}.csv',
    )


# ── Patient Dashboard Data ───────────────────────────────────────────────────


# Fallback adult reference ranges, applied when a lab entry has no `ref` from
# the source file. Keys are normalized lowercase marker names; values are
# either a single (lower, upper, unit) tuple or a dict mapping sex codes
# ('M' / 'W') to such a tuple for sex-specific ranges. Set lower or upper to
# None for one-sided ranges. Sources: institutional defaults commonly used in
# German clinical chemistry. Used only as a display fallback.
_LAB_FALLBACK_REFS = {
    # Markers present in the current dashboard data
    's100':       (None, 0.105, 'µg/l'),
    'ldh':        (135.0, 250.0, 'U/l'),
    'hemoglobin': {'M': (13.5, 17.5, 'g/dl'),
                   'W': (12.0, 16.0, 'g/dl')},
    'leukocytes': (3.6, 9.2, '/nl'),
    'ast':        {'M': (None, 50.0, 'U/l'),
                   'W': (None, 35.0, 'U/l')},
    'alt':        {'M': (None, 50.0, 'U/l'),
                   'W': (None, 35.0, 'U/l')},
    'ggt':        {'M': (None, 60.0, 'U/l'),
                   'W': (None, 40.0, 'U/l')},
    'crp':        (None, 0.5, 'mg/dl'),
    'creatinine': {'M': (0.9, 1.3, 'mg/dl'),
                   'W': (0.6, 1.1, 'mg/dl')},
    'egfr':       (60.0, None, 'ml/min/1,73qm'),
    'sodium':     (136.0, 145.0, 'mmol/l'),
    'potassium':  (3.5, 5.1, 'mmol/l'),
    'cortisol':   (171.0, 536.0, 'nmol/l'),
    'tsh':        (0.3, 3.0, 'mU/l'),
    'ft4':        (11.5, 22.7, 'pmol/l'),
    'nt-probnp':  (None, 125.0, 'pg/ml'),
    'ck':         {'M': (46.0, 171.0, 'U/l'),
                   'W': (34.0, 135.0, 'U/l')},
    'ck-mb':      (None, 25.0, 'U/l'),
}


def _resolve_fallback_entry(entry, sex):
    """Resolve a fallback table entry to (lo, hi, unit) given patient sex.

    If entry is sex-keyed, picks the matching code; falls back to the first
    available range if the sex is unknown or missing from the entry.
    """
    if entry is None:
        return None
    if isinstance(entry, dict):
        if sex and sex in entry:
            return entry[sex]
        # No sex info or unrecognized code — use whichever range is defined.
        return next(iter(entry.values()), None)
    return entry


def _format_fallback_ref(lo, hi, unit):
    """Render a fallback (lo, hi, unit) tuple as a display string + suffix."""
    if lo is not None and hi is not None:
        body = f"{_fmt_num(lo)}-{_fmt_num(hi)}"
    elif hi is not None:
        body = f"<{_fmt_num(hi)}"
    elif lo is not None:
        body = f">{_fmt_num(lo)}"
    else:
        return ''
    if unit:
        body = f"{body} {unit}"
    return f"{body} *"


def _fmt_num(n):
    if float(n).is_integer():
        return str(int(n))
    return f"{n:g}".replace('.', ',')


def _apply_fallback_refs(lab_values, sex=None):
    """Fill in `ref` for lab entries lacking a reference range, using the
    institutional fallback table. Marks fallback values with a trailing ' *'.
    `sex` ('M' / 'W') selects the appropriate range for sex-keyed entries.
    """
    sex_code = (sex or '').strip().upper() or None
    for lv in lab_values:
        if (lv.get('ref') or '').strip():
            continue
        marker = (lv.get('marker') or '').strip().lower()
        fb = _resolve_fallback_entry(_LAB_FALLBACK_REFS.get(marker), sex_code)
        if fb is None:
            continue
        lo, hi, unit = fb
        # Prefer the entry's own unit if present; fallback unit is only used
        # to make the range self-explanatory when the file omits one too.
        display_unit = (lv.get('unit') or '').strip() or unit
        rendered = _format_fallback_ref(lo, hi, display_unit)
        if rendered:
            lv['ref'] = rendered


def _load_dashboard_json(case_id):
    """Load a pre-processed dashboard JSON for a case from DASHBOARD_DIR."""
    path = os.path.join(DASHBOARD_DIR, f'{case_id}_dashboard.json')
    if os.path.isfile(path):
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # Sort lab values chronologically (DD.MM.YYYY), newest first
        def _date_sort_key(entry):
            try:
                d, m, y = entry.get('date', '').split('.')
                return (int(y), int(m), int(d))
            except (ValueError, AttributeError):
                return (0, 0, 0)
        if 'lab_values' in data:
            _apply_fallback_refs(data['lab_values'], data.get('sex'))
            data['lab_values'] = sorted(data['lab_values'], key=_date_sort_key, reverse=True)
        # Sort imaging chronologically, oldest first (ascending)
        if 'imaging' in data:
            data['imaging'] = sorted(data['imaging'], key=_date_sort_key)
        # Sort metastases chronologically, oldest first (ascending)
        # Supports DD.MM.YYYY and MM/YYYY; entries without dates go to the end
        if 'metastases_detail' in data:
            def _meta_sort_key(entry):
                d = (entry.get('date') or '').strip()
                m = _re.match(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', d)
                if m:
                    return (0, int(m.group(3)), int(m.group(2)), int(m.group(1)))
                m = _re.match(r'^(\d{1,2})/(\d{4})$', d)
                if m:
                    return (0, int(m.group(2)), int(m.group(1)), 0)
                m = _re.match(r'^(\d{4})$', d)
                if m:
                    return (0, int(m.group(1)), 0, 0)
                return (1, 0, 0, 0)
            data['metastases_detail'] = sorted(data['metastases_detail'], key=_meta_sort_key)
        # Sort therapies oldest-first (ascending) within each category
        if 'therapies' in data and isinstance(data['therapies'], dict):
            def _therapy_date_key(entry):
                # Use only the start portion of ranges like "04.05.2023 - 02.06.2023"
                d = _re.sub(r'^seit\s+', '', entry.get('date', '').strip())
                d = _re.split(r'\s*[-–]\s*', d)[0].strip()
                m = _re.match(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', d)
                if m:
                    return (int(m.group(3)), int(m.group(2)), int(m.group(1)))
                m = _re.match(r'^(\d{1,2})\.(\d{4})$', d)
                if m:
                    return (int(m.group(2)), int(m.group(1)), 0)
                m = _re.match(r'^(\d{1,2})/(\d{4})$', d)
                if m:
                    return (int(m.group(2)), int(m.group(1)), 0)
                return (0, 0, 0)
            for cat in ('surgery', 'radiation', 'systemic', 'other_locoregional', 'current'):
                if cat in data['therapies'] and isinstance(data['therapies'][cat], list):
                    data['therapies'][cat] = sorted(data['therapies'][cat], key=_therapy_date_key)
        return data
    return None


def _build_patient_data():
    """Build PATIENT_DATA dict by enumerating dashboard_data/<case_id>_dashboard.json."""
    result = {}
    if os.path.isdir(DASHBOARD_DIR):
        for fname in os.listdir(DASHBOARD_DIR):
            if fname.lower().endswith('_dashboard.json'):
                case_id = fname[:-len('_dashboard.json')]
                dash = _load_dashboard_json(case_id)
                if dash:
                    result[case_id] = dash
    return result


PATIENT_DATA = _build_patient_data()


# ── Ground-truth tooltip data ────────────────────────────────────────────────


def _fmt_kv_list(items, sep=', '):
    """Render a list of strings, dropping empties and stripping whitespace."""
    out = [str(x).strip() for x in items if x is not None and str(x).strip()]
    return sep.join(out)


def _fmt_staging(stg):
    """Render a staging dict like {'t':..,'n':..,'m':..,'uicc':..,'classification':..,'date':..}."""
    if not isinstance(stg, dict):
        return str(stg or '').strip()
    parts = []
    tnm = _fmt_kv_list([stg.get('t'), stg.get('n'), stg.get('m')], sep=' ')
    if tnm:
        parts.append(tnm)
    if stg.get('uicc'):
        parts.append(f"Stadium {stg['uicc']}")
    if stg.get('classification'):
        parts.append(f"({stg['classification']})")
    if stg.get('date'):
        parts.append(f"– {stg['date']}")
    return ' '.join(parts)


def _fmt_metastasis(m):
    parts = []
    label = (m.get('label') or '').strip()
    region = _de_region((m.get('region') or '').strip())
    date = (m.get('date') or '').strip()
    status = _de_status((m.get('status') or '').strip())
    if date:
        parts.append(date)
    if label:
        parts.append(label)
    elif region:
        parts.append(region)
    if status and status != 'aktiv':
        parts.append(f"[{status}]")
    return ' '.join(parts) if parts else ''


# Region codes used in the dashboard JSON → German display names.
_REGION_DE = {
    'head':         'Kopf',
    'brain':        'Gehirn',
    'neck':         'Hals',
    'thorax':       'Thorax',
    'chest':        'Thorax',
    'chest_left':   'Thorax links',
    'chest_right':  'Thorax rechts',
    'lung':         'Lunge',
    'lung_left':    'Lunge links',
    'lung_right':   'Lunge rechts',
    'liver':        'Leber',
    'abdomen':      'Abdomen',
    'pelvis':       'Becken',
    'bone':         'Knochen',
    'skin':         'Haut',
    'in_transit':   'In-Transit',
    'lymph_nodes':  'Lymphknoten',
    'lymph':        'Lymphknoten',
    'lk':           'Lymphknoten',
    'thigh':        'Oberschenkel',
    'thigh_left':   'Oberschenkel links',
    'thigh_right':  'Oberschenkel rechts',
    'arm':          'Arm',
    'arm_left':     'Arm links',
    'arm_right':    'Arm rechts',
    'leg':          'Bein',
    'leg_left':     'Bein links',
    'leg_right':    'Bein rechts',
    'back':         'Rücken',
    'spine':        'Wirbelsäule',
    'adrenal':      'Nebenniere',
    'kidney':       'Niere',
    'spleen':       'Milz',
    'pancreas':     'Pankreas',
    'breast':       'Brust',
    'other':        'Sonstige',
}

_STATUS_DE = {
    'active':   'aktiv',
    'inactive': 'inaktiv',
    'resected': 'reseziert',
    'stable':   'stabil',
    'progressive': 'progredient',
    'regressive':  'regredient',
}


def _de_region(code):
    """Translate region codes (lung, head, …) to German display labels."""
    if not code:
        return ''
    return _REGION_DE.get(code.lower(), code)


def _de_status(code):
    """Translate metastasis status codes to German."""
    if not code:
        return ''
    return _STATUS_DE.get(code.lower(), code)


def _fmt_therapy(t):
    parts = []
    date = (t.get('date') or '').strip()
    name = (t.get('name') or t.get('label') or t.get('regimen') or '').strip()
    note = (t.get('note') or t.get('reason') or '').strip()
    if date:
        parts.append(date)
    if name:
        parts.append(name)
    if note:
        parts.append(f"({note})")
    return ' '.join(parts).strip()


def _extract_dashboard_excerpts(d):
    """Build a slug → list[str] dict of ground-truth values from a dashboard.

    Only INFO_ITEMS slugs that have an obvious mapping into the structured
    dashboard JSON are populated. Items without a clean mapping are skipped
    (the protocol excerpts still cover them).
    """
    if not isinstance(d, dict):
        return {}

    out = {}

    def add(slug, value):
        if value is None:
            return
        if isinstance(value, list):
            value = [v for v in value if v]
            if not value:
                return
            out.setdefault(slug, []).extend(str(v) for v in value)
        else:
            s = str(value).strip()
            if s:
                out.setdefault(slug, []).append(s)

    # ── Demographie ──
    dob = (d.get('dob') or '').strip()
    age = d.get('age')
    if age or dob:
        add(slugify('Demographie: Alter'),
            f"{age} J." + (f" (geb. {dob})" if dob else '') if age else f"geb. {dob}")
    if d.get('sex'):
        sx = {'M': 'männlich', 'W': 'weiblich'}.get(str(d['sex']).strip().upper(), str(d['sex']))
        add(slugify('Demographie: Geschlecht'), sx)

    # ── Allgemeinzustand ──
    if d.get('comorbidities'):
        add(slugify('Allgemeinzustand: Komorbiditäten'), d['comorbidities'])
    fz = []
    if d.get('ecog'):       fz.append(f"ECOG {d['ecog']}")
    if d.get('karnofsky'):  fz.append(f"Karnofsky {d['karnofsky']}")
    if fz:
        add(slugify('Allgemeinzustand: Funktioneller Zustand (e.g. ECOG, Karnofsky)'),
            ' · '.join(fz))

    # ── Primärtumor ──
    add(slugify('Primärtumor: Datum der Erstdiagnose'),    d.get('diagnosis_date'))
    pdx = _fmt_kv_list([d.get('primary_diagnosis'), d.get('histology')], sep=' — ')
    add(slugify('Primärtumor: Art des Tumors'), pdx)
    add(slugify('Primärtumor: Lokalisation'),              d.get('primary_location'))
    add(slugify('Primärtumor: Tumordicke'),                d.get('tumor_thickness'))
    add(slugify('Primärtumor: Ulzeration'),                d.get('ulceration'))
    add(slugify('Primärtumor: Mitotische Aktivität'),      d.get('mitoses'))
    add(slugify('Primärtumor: Stadium der Erkrankung bei Erstdiagnose'),
        _fmt_staging(d.get('initial_staging')))
    muts = d.get('mutations')
    if isinstance(muts, dict):
        # Always report the clinically relevant melanoma triad (BRAF / NRAS /
        # KIT) including wildtype status; list any other notable variants
        # afterwards. "Nicht bestimmt" entries are skipped entirely.
        WILDTYPE = ('wildtyp', 'wild type', 'wildtype')
        UNKNOWN  = ('nicht bestimmt', '')
        primary_keys = ('BRAF', 'NRAS', 'KIT')
        primary = []
        for g in primary_keys:
            v = (muts.get(g) or '').strip()
            if v and v.lower() not in UNKNOWN:
                primary.append(f"{g}: {v}")
        extras = []
        for g, v in muts.items():
            if g in primary_keys: continue
            sv = (str(v) or '').strip()
            if not sv or sv.lower() in WILDTYPE + UNKNOWN: continue
            extras.append(f"{g}: {sv}")
        rendered = ', '.join(primary + extras)
        if rendered:
            add(slugify('Primärtumor: Mutationsstatus (e.g. BRAF)'), rendered)
    add(slugify('Primärtumor: PD-L1 Status'),              d.get('pdl1'))

    # ── Primärtherapie ──
    add(slugify('Primärtherapie: Resektionsstatus'),       d.get('resection_status'))
    add(slugify('Primärtherapie: Sicherheitsabstand'),     d.get('safety_margin'))
    add(slugify('Primärtherapie: SLNE'),                   d.get('slne'))

    # ── Aktueller Status ──
    add(slugify('Aktueller Status: Krankheitsstatus (unverändert, progredient, regredient)'),
        d.get('disease_status'))
    add(slugify('Aktueller Status: Stadium der Erkrankung'),
        _fmt_staging(d.get('staging')))

    mets = d.get('metastases_detail') or []
    if mets:
        rendered_mets = [_fmt_metastasis(m) for m in mets if isinstance(m, dict)]
        rendered_mets = [m for m in rendered_mets if m]
        if rendered_mets:
            add(slugify('Aktueller Status: Metastasierung'), rendered_mets)
            locs = list(dict.fromkeys(  # preserve order, dedupe
                _de_region((m.get('region') or '').strip())
                for m in mets if isinstance(m, dict)
            ))
            locs = [l for l in locs if l]
            if locs:
                add(slugify('Aktueller Status: Lokalisation der Metastasierung'),
                    ', '.join(locs))

    # ── Krankheitsverlauf (date of first LK / Fernmet) ──
    lk_dates  = sorted({(m.get('date') or '').strip()
                        for m in mets if isinstance(m, dict)
                        and (m.get('region') or '').lower() in ('lymph_nodes', 'lymph', 'lk')
                        and (m.get('date') or '').strip()})
    far_dates = sorted({(m.get('date') or '').strip()
                        for m in mets if isinstance(m, dict)
                        and (m.get('region') or '').lower() not in ('lymph_nodes', 'lymph', 'lk', 'skin', 'in_transit')
                        and (m.get('date') or '').strip()})
    if lk_dates:
        add(slugify('Krankheitsverlauf: Datum Erstdiagnose Lymphknotenmetastasierung (i.e., Stadium III)'),
            lk_dates[0])
    if far_dates:
        add(slugify('Krankheitsverlauf: Datum Erstdiagnose Fernmetastasen (i.e., Stadium IV)'),
            far_dates[0])

    # ── Aktuelle Befunde ──
    imgs = d.get('imaging') or []
    if imgs:
        rendered_imgs = []
        for im in imgs:
            if not isinstance(im, dict): continue
            line = _fmt_kv_list([im.get('date'), im.get('modality'),
                                 im.get('region'), im.get('finding')], sep=' · ')
            if line:
                rendered_imgs.append(line)
        if rendered_imgs:
            add(slugify('Aktuelle Befunde: Bildgebende Verfahren (e.g. CT, MRT, PET-CT, LK-Sono)'),
                rendered_imgs[-6:])  # cap to most recent 6

    labs = d.get('lab_values') or []
    if labs:
        # Prioritise tumour-marker labs (S100, LDH) so they're never cut off
        # by the display cap, then include other markers in original order.
        PRIORITY = ('s100', 'ldh')
        priority_labs = [lv for lv in labs if isinstance(lv, dict)
                         and (lv.get('marker') or '').strip().lower() in PRIORITY]
        other_labs    = [lv for lv in labs if isinstance(lv, dict)
                         and (lv.get('marker') or '').strip().lower() not in PRIORITY]
        ordered_labs = priority_labs + other_labs
        rendered_labs = []
        for lv in ordered_labs:
            line = _fmt_kv_list([lv.get('date'), lv.get('marker'),
                                 _fmt_kv_list([lv.get('value'), lv.get('unit')], sep=' '),
                                 f"(Ref. {lv['ref']})" if (lv.get('ref') or '').strip() else ''], sep=' · ')
            if line:
                rendered_labs.append(line)
        if rendered_labs:
            add(slugify('Aktuelle Befunde: Laborwerte (e.g. S100, LDH, HLA-A2)'),
                rendered_labs)

    # ── Therapieverlauf ──
    th = d.get('therapies') or {}
    if isinstance(th, dict):
        for cat_key, slug_label in (
            ('radiation',           'Therapieverlauf: Strahlentherapie'),
            ('other_locoregional',  'Therapieverlauf: Andere lokoregionäre Therapien (e.g. IL-2, T-VEC)'),
            ('systemic',            'Therapieverlauf: Systemtherapie'),
            ('current',             'Therapieverlauf: Aktuelle Therapie'),
        ):
            entries = th.get(cat_key) or []
            if not isinstance(entries, list): continue
            rendered = [_fmt_therapy(t) for t in entries if isinstance(t, dict)]
            rendered = [r for r in rendered if r]
            if rendered:
                add(slugify(slug_label), rendered)

    return out


def _build_ground_truth_excerpts(case_id, protocol_excerpts=None):
    """Return slug → list[{label, items}] of ground-truth values from the
    dashboard JSON only. The ``protocol_excerpts`` argument is accepted for
    backward compatibility but currently ignored — the tooltip surfaces only
    structured dashboard fields.
    """
    dash = PATIENT_DATA.get(case_id)
    if not dash:
        return {}
    dash_excerpts = _extract_dashboard_excerpts(dash)
    return {
        slug: [{'label': 'Dashboard', 'items': items}]
        for slug, items in dash_excerpts.items()
        if items
    }


def _read_original_document(case_id):
    """Read the original TXT document for a case from DOCUMENTS_DIR."""
    txt_path = os.path.join(DOCUMENTS_DIR, f'{case_id}.txt')
    if os.path.isfile(txt_path):
        return _read_text_file(txt_path)
    return ''


_TB_DATE_RE = _re.compile(
    r'Interdisziplin[äa]res\s+Tumorboard\s+[\wÄÖÜäöüß\-]+\s+'
    r'(?P<date>\d{2}\.\d{2}\.\d{4})\s+(?P<time>\d{2}:\d{2})',
    _re.IGNORECASE,
)

def _extract_tb_conference(original_doc):
    """Return (date, time, board_label, weekday) tuple from the protocol header,
    or (None, None, None, None) if not found."""
    if not original_doc:
        return (None, None, None, None)
    head = '\n'.join(original_doc.splitlines()[:40])
    m = _TB_DATE_RE.search(head)
    if not m:
        return (None, None, None, None)
    label_m = _re.search(
        r'Interdisziplin[äa]res\s+Tumorboard\s+(?P<label>[\wÄÖÜäöüß\-]+)',
        head, _re.IGNORECASE,
    )
    label = label_m.group('label') if label_m else ''
    date_str = m.group('date')
    weekday = None
    try:
        dt = datetime.strptime(date_str, '%d.%m.%Y')
        weekday = ['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag',
                   'Freitag', 'Samstag', 'Sonntag'][dt.weekday()]
    except ValueError:
        pass
    return (date_str, m.group('time'), label, weekday)


@app.route('/evaluate/<case_id>/dashboard', methods=['GET', 'POST'])
@login_required
def case_dashboard(case_id):
    """Patient dashboard shown before evaluation steps for each case."""
    if case_id not in PATIENT_DATA:
        return redirect(url_for('evaluate_resume'))

    username = session['username']
    is_annotator = _get_user_role(username) == 'annotator'

    # Annotators get a read-only view (no rating rail, no continue button,
    # no evaluator state initialisation, no POST handling).
    if is_annotator:
        patient = PATIENT_DATA[case_id]
        original_doc = _read_original_document(case_id)
        tb_date, tb_time, tb_label, tb_weekday = _extract_tb_conference(original_doc)
        return render_template('case_dashboard.html',
                               case_id=case_id,
                               case_index=0,
                               patient=patient,
                               original_doc=original_doc,
                               done_cases=0,
                               total_cases=0,
                               first_visit=False,
                               info_items=INFO_ITEMS,
                               slugify=slugify,
                               tb_date=tb_date,
                               tb_time=tb_time,
                               tb_label=tb_label,
                               tb_weekday=tb_weekday,
                               relevance_existing={},
                               annotator_mode=True)

    texts = load_texts()
    user_data = init_evaluator(username, texts)
    case_order = user_data.get('case_order', [])
    case_index = case_order.index(case_id) if case_id in case_order else 0

    if request.method == 'POST':
        user_data = load_responses(username)
        # Server-side guard: do not let users leave the dashboard for the
        # first time until every INFO_ITEM has been rated. The rail UI
        # enforces this client-side, but a hand-crafted POST could bypass it.
        case_ratings = user_data.get('ratings', {}).get(case_id, {})
        rel = case_ratings.get('case_relevance', {}) or {}
        unrated = [item for item in INFO_ITEMS
                   if rel.get('relevant_' + slugify(item)) not in ('yes', 'no')]
        if unrated:
            return redirect(url_for('case_dashboard', case_id=case_id))
        dashboards_seen = user_data.get('dashboards_seen', [])
        first_time = case_id not in dashboards_seen
        if first_time:
            dashboards_seen.append(case_id)
        user_data['dashboards_seen'] = dashboards_seen
        save_responses(username, user_data)
        if first_time:
            _send_notification_email(
                subject=f'[tumor_board_eval] Fall gestartet: {username}',
                body=(
                    f'Benutzer:  {username}\n'
                    f'Fall-ID:   {case_id}\n'
                    f'Fall-Nr.:  {case_index + 1} / {len(case_order)}\n'
                    f'Zeitpunkt: {datetime.utcnow().isoformat()} UTC\n'
                ),
            )
        return redirect(url_for('evaluate_resume'))

    patient = PATIENT_DATA[case_id]
    original_doc = _read_original_document(case_id)
    tb_date, tb_time, tb_label, tb_weekday = _extract_tb_conference(original_doc)

    # First visit = the user has not yet pressed "Weiter zur Bewertung" for
    # this case. Subsequent visits (via the protocol button on an eval page)
    # show a "Zurück zur Bewertung" link in the header instead of the bottom
    # continue button.
    case_ratings_pre = user_data.get('ratings', {}).get(case_id, {})
    relevance_pre = case_ratings_pre.get('case_relevance', {}) or {}
    first_visit = (case_id not in user_data.get('dashboards_seen', [])
                   or not any(relevance_pre.get('relevant_' + slugify(item)) in ('yes', 'no')
                              for item in INFO_ITEMS))

    # Compute progress for the header
    done_cases = sum(
        1 for cid in case_order
        if all(is_step_done(user_data, cid, i) for i in range(len(RATING_STEPS)))
    )
    total_cases = len(case_order)

    # Existing relevance ratings for the dashboard rating rail (Option A).
    case_ratings = user_data.get('ratings', {}).get(case_id, {})
    relevance_existing = case_ratings.get('case_relevance', {}) or {}

    return render_template('case_dashboard.html',
                           case_id=case_id,
                           case_index=case_index,
                           patient=patient,
                           original_doc=original_doc,
                           done_cases=done_cases,
                           total_cases=total_cases,
                           first_visit=first_visit,
                           info_items=INFO_ITEMS,
                           slugify=slugify,
                           tb_date=tb_date,
                           tb_time=tb_time,
                           tb_label=tb_label,
                           tb_weekday=tb_weekday,
                           relevance_existing=relevance_existing)


@app.route('/api/original-doc/<case_id>')
@login_required
def api_original_doc(case_id):
    """Return the raw original document text for a case (for modal display)."""
    if case_id not in PATIENT_DATA:
        return {'error': 'Not found'}, 404
    doc = _read_original_document(case_id)
    return {'text': doc}


@app.route('/api/case-relevance/<case_id>', methods=['POST'])
@login_required
def api_case_relevance(case_id):
    """Persist a single relevance toggle from the dashboard sidebar.

    Body (JSON or form): { slug: <info-item slug>, value: 'yes'|'no'|'' }
    Returns { ok: true, rated: int, total: int, done: bool }.
    """
    if case_id not in PATIENT_DATA:
        return {'error': 'Not found'}, 404

    payload = request.get_json(silent=True) or request.form
    slug    = (payload.get('slug') or '').strip()
    value   = (payload.get('value') or '').strip()

    valid_slugs = {slugify(item) for item in INFO_ITEMS}
    if slug not in valid_slugs:
        return {'error': 'Invalid slug'}, 400
    if value not in ('yes', 'no', ''):
        return {'error': 'Invalid value'}, 400

    username  = session['username']
    with _responses_lock(username):
        user_data = load_responses(username)
        ratings   = user_data.setdefault('ratings', {})
        case_r    = ratings.setdefault(case_id, {})
        rel       = case_r.setdefault('case_relevance', {})

        field = 'relevant_' + slug
        if value == '':
            rel.pop(field, None)
        else:
            rel[field] = value
        rel.setdefault('comment', '')

        save_responses(username, user_data)

        rated = sum(1 for item in INFO_ITEMS
                    if rel.get('relevant_' + slugify(item)) in ('yes', 'no'))
    total = len(INFO_ITEMS)
    return {'ok': True, 'rated': rated, 'total': total, 'done': rated == total}


@app.route('/api/imaging-pdf/<case_id>')
@login_required
def imaging_pdf(case_id):
    """Serve an imaging PDF from imaging/<case_id>_<modality>/<file>.pdf."""
    if case_id not in PATIENT_DATA:
        return 'Not found', 404
    img_dir = request.args.get('dir', '')
    pdf_file = request.args.get('file', '')
    if not img_dir or not pdf_file:
        return 'Missing parameters', 400
    # Sanitise to prevent path traversal
    img_dir = os.path.basename(img_dir)
    pdf_file = os.path.basename(pdf_file)
    if not pdf_file.lower().endswith('.pdf'):
        return 'Invalid file', 400
    pdf_path = os.path.join(IMAGING_DIR, img_dir, pdf_file)
    if not os.path.isfile(pdf_path):
        return 'PDF nicht gefunden', 404
    resp = send_file(pdf_path, mimetype='application/pdf')
    resp.headers['Content-Disposition'] = 'inline'
    return resp


@app.route('/api/imaging-txt/<case_id>')
@login_required
def imaging_txt(case_id):
    """Return the raw text content of an imaging report."""
    if case_id not in PATIENT_DATA:
        return {'error': 'Not found'}, 404
    # Find the matching imaging entry by dir + date + type
    img_dir = request.args.get('dir', '')
    date = request.args.get('date', '')
    img_type = request.args.get('type', '')
    if not img_dir:
        return {'error': 'Missing parameters'}, 400
    patient = PATIENT_DATA[case_id]
    for img in patient.get('imaging', []):
        if img.get('img_dir', '') == img_dir and img.get('date', '') == date and img.get('type', '') == img_type:
            return {'text': img.get('finding', '')}
    return {'text': ''}


# ── annotator (researcher) UI ────────────────────────────────────────────────

ANNOTATE_TEXT_KEYS = [
    # (text_key, label, case_dict_src_key, sidebar_letter)
    ('human_summary', 'Human · Zusammenfassung', 'human_summary', 'H'),
    ('llm_summary',   'LLM · Zusammenfassung',   'llm_summary',   'L'),
]


def _annotation_progress(ann):
    """Return {text_key: (rated_count, total)} for a loaded annotation dict."""
    total = len(INFO_ITEMS)
    out = {}
    texts = ann.get('texts') or {}
    for tk, _label, _src_key, _letter in ANNOTATE_TEXT_KEYS:
        items = (texts.get(tk) or {}).get('items') or {}
        rated = sum(1 for it in items.values() if it.get('status'))
        out[tk] = (rated, total)
    return out


@app.route('/annotate')
@annotator_required
def annotate_index():
    texts = load_texts()
    cases = []
    for t in texts:
        ann = _load_annotation(str(t['id']))
        prog = _annotation_progress(ann)
        cases.append({
            'id': str(t['id']),
            'title': t.get('title') or t.get('case_label') or str(t['id'])[:10],
            'progress': prog,
            'updated_at': ann.get('updated_at'),
        })
    return render_template('annotate_index.html',
                           cases=cases,
                           text_keys=ANNOTATE_TEXT_KEYS,
                           total_items=len(INFO_ITEMS))


@app.route('/annotate/<case_id>')
@annotator_required
def annotate_case(case_id):
    texts = load_texts()
    case = next((t for t in texts if str(t['id']) == case_id), None)
    if case is None:
        return redirect(url_for('annotate_index'))
    ann = _load_annotation(case_id)
    text_blocks = []
    for tk, label, src_key, letter in ANNOTATE_TEXT_KEYS:
        text_blocks.append({
            'key': tk,
            'label': label,
            'letter': letter,
            'content': case.get(src_key, '') or '',
        })
    # Build per-text per-slug status map for sidebar rendering
    status_map = {}
    for tk, _label, _src_key, _letter in ANNOTATE_TEXT_KEYS:
        items = ((ann.get('texts') or {}).get(tk) or {}).get('items') or {}
        status_map[tk] = {slug: it.get('status') for slug, it in items.items()}
    return render_template('annotate.html',
                           case_id=case_id,
                           case_label=case.get('title') or case_id[:10],
                           text_blocks=text_blocks,
                           info_items=INFO_ITEMS,
                           slugify=slugify,
                           annotation=ann,
                           status_map=status_map)


@app.route('/annotate/<case_id>/save', methods=['POST'])
@annotator_required
def annotate_save(case_id):
    """Replace one INFO_ITEM's annotation for one text.

    Body JSON: { text_key, slug, status, spans: [text, ...] }
    """
    payload = request.get_json(silent=True) or {}
    text_key = payload.get('text_key')
    slug     = payload.get('slug')
    status   = payload.get('status')  # 'enthalten' | 'nicht_enthalten' | 'falsch' | None
    spans    = payload.get('spans') or []

    if not text_key or not slug:
        return {'ok': False, 'error': 'text_key and slug required'}, 400
    if text_key not in {tk for tk, _l, _s, _t in ANNOTATE_TEXT_KEYS}:
        return {'ok': False, 'error': 'unknown text_key'}, 400
    if status not in (None, 'enthalten', 'nicht_enthalten', 'falsch'):
        return {'ok': False, 'error': 'invalid status'}, 400

    ann = _load_annotation(case_id)
    block = ann['texts'].setdefault(text_key, {'items': {}})
    items = block.setdefault('items', {})
    if status is None and not spans:
        items.pop(slug, None)
    else:
        items[slug] = {'status': status,
                       'spans': [s for s in spans if s],
                       'origin': 'human'}
    _save_annotation(case_id, ann, username=session.get('username'))
    return {'ok': True, 'progress': _annotation_progress(ann)[text_key]}


@app.route('/annotate/<case_id>/reset', methods=['POST'])
@annotator_required
def annotate_reset(case_id):
    """Delete the saved annotation so the next load re-seeds from LLM."""
    p = _annotation_path(case_id)
    if os.path.exists(p):
        try:
            os.remove(p)
        except OSError as e:
            return {'ok': False, 'error': str(e)}, 500
    return {'ok': True}


@app.route('/annotate/<case_id>/llm-review', methods=['POST'])
@annotator_required
def annotate_llm_review(case_id):
    """Run an LLM fact-check of one text against the original protocol and
    overwrite that text's items in the annotation file.

    Body JSON: {"text_key": "<llm_summary|human_summary|llm_problem|human_problem>"}
    """
    import llm_review

    payload = request.get_json(silent=True) or {}
    text_key = payload.get('text_key')
    mode     = payload.get('mode', 'merge')  # 'merge' or 'overwrite'
    if mode not in ('merge', 'overwrite'):
        return {'ok': False, 'error': 'invalid mode'}, 400
    valid_keys = {tk for tk, _l, _s, _t in ANNOTATE_TEXT_KEYS}
    if text_key not in valid_keys:
        return {'ok': False, 'error': 'unknown text_key'}, 400

    texts = load_texts()
    case = next((t for t in texts if str(t['id']) == case_id), None)
    if case is None:
        return {'ok': False, 'error': 'case not found'}, 404

    src_key = next(sk for tk, _l, sk, _t in ANNOTATE_TEXT_KEYS if tk == text_key)
    summary = case.get(src_key, '') or ''
    if not summary.strip():
        return {'ok': False, 'error': 'text is empty'}, 400

    protocol = _read_original_document(case_id)
    if not protocol.strip():
        return {'ok': False, 'error': 'original protocol not available'}, 400

    try:
        new_items = llm_review.review_text(protocol, summary, INFO_ITEMS)
    except Exception as e:
        return {'ok': False, 'error': f'LLM review failed: {e}'}, 500

    ann = _load_annotation(case_id)
    block = ann.setdefault('texts', {}).setdefault(text_key, {'items': {}})
    existing = block.setdefault('items', {})

    written = 0
    skipped_human = 0
    if mode == 'overwrite':
        block['items'] = {
            slug: {**payload_, 'origin': 'llm_review'}
            for slug, payload_ in new_items.items()
        }
        written = len(new_items)
    else:  # merge: leave human-touched items alone
        for slug, payload_ in new_items.items():
            cur = existing.get(slug)
            if cur and cur.get('origin') == 'human':
                skipped_human += 1
                continue
            existing[slug] = {**payload_, 'origin': 'llm_review'}
            written += 1

    ann.setdefault('llm_review', {})[text_key] = datetime.utcnow().isoformat()
    _save_annotation(case_id, ann, username=session.get('username'))
    rated, total = _annotation_progress(ann)[text_key]
    return {'ok': True, 'rated': rated, 'total': total,
            'written': written, 'skipped_human': skipped_human, 'mode': mode}


if __name__ == '__main__':
    app.run(debug=True, port=5001)
