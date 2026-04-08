from flask import (Flask, render_template, request, redirect, url_for,
                   session, send_file)
import json, csv, io, os, random
from werkzeug.security import check_password_hash
from functools import wraps
from datetime import datetime
from markupsafe import Markup, escape

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-in-production-please')

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
TEXTS_FILE      = os.path.join(BASE_DIR, 'texts.json')
RESPONSES_DIR   = os.path.join(BASE_DIR, 'responses')
USERS_FILE      = os.path.join(BASE_DIR, 'users.json')
DOCUMENTS_DIR   = os.path.join(BASE_DIR, 'original_documents')
GUIDELINE_DIR   = os.path.join(BASE_DIR, 'guideline')
TEXTS_HUMAN_DIR = os.path.join(BASE_DIR, 'texts_human')
TEXTS_LLM_DIR   = os.path.join(BASE_DIR, 'texts_llm')
EXPORTS_DIR = os.path.join(BASE_DIR, 'exports')
HIGHLIGHT_FILE  = os.path.join(BASE_DIR, 'highlight_mappings.json')
import re as _re

# ── constants ────────────────────────────────────────────────────────────────

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

# section  = display name in stepper
# subtitle = short description
# key      = storage key
# tab      = True means show tab A / tab B; False = single form (side-by-side or no text)
# section_type = 'summary' | 'problem' | 'both'
RATING_STEPS = [
    {
        "index": 0,
        "key":   "case_relevance",
        "section": "Zusammenfassung",
        "subtitle": "Relevanz",
        "has_tabs": False,
        "section_type": "both",
        "alert": "Bitte beurteilen Sie für jede Information innerhalb einer Informationskategorie, ob diese Information für die Tumorboardanmeldung dieses Falls klinisch relevant ist. Diese Einschätzung gilt für den gesamten Fall.",
    },
    {
        "index": 1,
        "key":   "summary_integrity",
        "section": "Zusammenfassung",
        "subtitle": "Informationsgehalt",
        "has_tabs": True,
        "section_type": "summary",
        "alert": "Bitte beurteilen Sie für jede der folgenden Informationskategorien, ob die Information in dieser Version der Zusammenfassung enthalten ist.",
    },
    {
        "index": 1,
        "key":   "summary_falseinfo",
        "section": "Zusammenfassung",
        "subtitle": "Falschinformationen",
        "has_tabs": True,
        "section_type": "summary",
        "alert": "Wählen Sie alle Informationen aus, die in der Zusammenfassung enthalten, aber inhaltlich falsch oder irreführend sind. Bewerten Sie dann die möglichen Folgen und ihre Eintrittswahrscheinlichkeit.",
    },
    {
        "index": 1,
        "key":   "summary_missinginfo",
        "section": "Zusammenfassung",
        "subtitle": "Fehlende Informationen",
        "has_tabs": True,
        "section_type": "summary",
        "alert": "Wählen Sie alle Informationen aus, die als klinisch relevant eingestuft, aber in der Zusammenfassung nicht enthalten sind. Bewerten Sie dann die möglichen Folgen und ihre Eintrittswahrscheinlichkeit.",
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
        "alert": "Bitte geben Sie den aktuellen Krankheitsstatus an und wählen Sie die in der Fragestellung vorgeschlagenen Diskussionsschwerpunkte.",
        "status_options": ["Unverändert", "Progredient", "Remission"],
        "topic_options": [
            "Weitere Diagnostik",
            "Therapie (Beginn, Auswahl, Modifikation)",
            "Verlaufskontrolle",
            "Organisatorische Fragen",
            "Keine Spezifizierung"
        ],
    },
    {
        "index": 9,
        "key":   "problem_correctness",
        "section": "Fragestellung",
        "subtitle": "Leitlinienkonformität",
        "has_tabs": True,
        "section_type": "problem",
        "alert": "Vergleichen Sie die Empfehlung in der Fragestellung mit der S3-Leitlinie für die Melanomtherapie.",
        "options": ["Konkordant", "Korrekte Alternative", "Inkorrekte Empfehlung"],
    },
    {
        "index": 10,
        "key":   "problem_specificity",
        "section": "Fragestellung",
        "subtitle": "Fallspezifität",
        "has_tabs": True,
        "section_type": "problem",
        "alert": "Beurteilen Sie den Grad der Spezifität der Fragestellung für die Tumorkonferenz.",
        "options": ["Zu generisch", "Optimal", "Zu spezifisch"],
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
    "Verlaufskontrolle",
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
    """Load pre-generated highlight mappings (slug → [excerpt, …])."""
    if os.path.exists(HIGHLIGHT_FILE):
        with open(HIGHLIGHT_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def _annotate_highlights(raw_text, mapping):
    """Apply _bold_headers then wrap mapped excerpts in <span> elements.

    *mapping* is {slug: [excerpt, …]} where excerpts are from the raw text.
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
        parts.append(
            f'<span class="hl-info" {slug_attr}>{escaped_excerpt}</span>'
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
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _read_text_file(path):
    """Read a UTF-8 text file and return its content, or empty string."""
    if path and os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    return ''


def _discover_cases():
    """Discover cases from original_documents/ PDFs.
    Returns sorted list of (fall_number, pdf_filename) tuples.
    """
    cases = []
    if not os.path.isdir(DOCUMENTS_DIR):
        return cases
    for fname in os.listdir(DOCUMENTS_DIR):
        if not fname.lower().endswith('.pdf'):
            continue
        m = _re.match(r'[Ff]all(\d+)', fname)
        if m:
            cases.append((int(m.group(1)), fname))
    cases.sort(key=lambda x: x[0])
    return cases


def _find_text_file(directory, pattern_prefix):
    """Find a text file in directory whose name starts with pattern_prefix (case-insensitive)."""
    if not os.path.isdir(directory):
        return None
    prefix_lower = pattern_prefix.lower()
    for fname in os.listdir(directory):
        if fname.lower().startswith(prefix_lower) and fname.lower().endswith('.txt'):
            return os.path.join(directory, fname)
    return None


def load_texts():
    """Dynamically build case list from original_documents/ PDFs and text files."""
    cases = _discover_cases()
    if not cases:
        # Fallback to texts.json if no PDFs found
        return load_json(TEXTS_FILE, [])

    texts = []
    for fall_nr, pdf_name in cases:
        prefix = f'fall_{fall_nr}'
        human_summary = _read_text_file(
            _find_text_file(os.path.join(TEXTS_HUMAN_DIR, 'zusammenfassung'), prefix))
        human_problem = _read_text_file(
            _find_text_file(os.path.join(TEXTS_HUMAN_DIR, 'fragestellung'), prefix))
        llm_summary = _read_text_file(
            _find_text_file(os.path.join(TEXTS_LLM_DIR, 'zusammenfassungen'), prefix))
        llm_problem = _read_text_file(
            _find_text_file(os.path.join(TEXTS_LLM_DIR, 'fragestellungen'), prefix))
        texts.append({
            'id': fall_nr,
            'pdf': pdf_name,
            'human_summary': human_summary,
            'llm_summary': llm_summary,
            'human_problem': human_problem,
            'llm_problem': llm_problem,
        })
    return texts


def _responses_path(username):
    """Return the path to a per-user responses file."""
    os.makedirs(RESPONSES_DIR, exist_ok=True)
    return os.path.join(RESPONSES_DIR, f'responses_{username}.json')


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
    """Create evaluator entry if not present, return user data dict."""
    user_data = load_responses(username)
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
    else:
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
            user_data['consent_given'] = True
            user_data['page'] = 'demographics'
            save_responses(username, user_data)
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

    # Compute "enthalten" items for falseinfo step (from tab0 rating of integrity)
    enthalten_items = []
    hl_version_key = None
    if key in ('summary_falseinfo', 'summary_integrity'):
        # Determine which version (human/llm) is shown for highlight mappings
        assign = assign_sum
        if tab_idx == 0:
            hl_version_key = 'human_summary' if assign == 'human_first' else 'llm_summary'
        else:
            hl_version_key = 'llm_summary' if assign == 'human_first' else 'human_summary'
    if key == 'summary_falseinfo':
        integrity_tab = case_ratings.get('summary_integrity_tab' + str(tab_idx), {})
        for item in INFO_ITEMS:
            slug = slugify(item)
            if integrity_tab.get('enthalten_' + slug) == 'yes':
                enthalten_items.append(item)

    # Compute "missing" items for missinginfo step (relevant + nicht enthalten)
    missing_items = []
    if key == 'summary_missinginfo':
        relevance = case_ratings.get('case_relevance', {})
        integrity_tab = case_ratings.get('summary_integrity_tab' + str(tab_idx), {})
        for item in INFO_ITEMS:
            slug = slugify(item)
            if (relevance.get('relevant_' + slug) == 'yes'
                    and integrity_tab.get('enthalten_' + slug) == 'no'):
                missing_items.append(item)

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
    if key in ('summary_falseinfo', 'summary_integrity') and text_content and hl_version_key:
        hl_mappings = _load_highlight_mappings()
        case_hl = hl_mappings.get(case_id, {}).get(hl_version_key, {})
        rendered_text = _annotate_highlights(text_content, case_hl)
        if key == 'summary_falseinfo':
            protocol_excerpts = hl_mappings.get(case_id, {}).get('protocol', {})
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
        protocol_excerpts=protocol_excerpts,
        slugify=slugify,
        rating_steps=RATING_STEPS,
        all_prior_done=all_prior_done,
        missing_step_names=missing_step_names,
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
            val = form.get('choice')
            if not val:
                return None, 'Bitte wählen Sie eine Bewertung aus.'
            rating['choice'] = val
            rating['comment'] = form.get('comment', '').strip()
            for item in INFO_ITEMS:
                slug = slugify(item)
                rating['relevant_' + slug] = form.get('relevant_' + slug, '')

        elif key in ('summary_completeness', 'summary_conciseness'):
            val = form.get('choice')
            if not val:
                return None, 'Bitte wählen Sie eine Bewertung aus.'
            rating['choice'] = val
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

        elif key == 'summary_integrity':
            missing = []
            for item in INFO_ITEMS:
                slug = slugify(item)
                val = form.get('enthalten_' + slug, '')
                rating['enthalten_' + slug] = val
                if val not in ('yes', 'no'):
                    missing.append(item)
            if missing:
                return None, f'Bitte bewerten Sie alle Informationen. Es fehlen noch {len(missing)} Bewertung(en).'
            rating['manual_annotations'] = form.get('manual_annotations', '{}')

        elif key == 'summary_falseinfo':
            false_items = form.getlist('false_item')
            rating['false_items'] = false_items
            for item in false_items:
                slug = slugify(item)
                severity = form.get('severity_' + slug, '')
                prob     = form.get('prob_' + slug, '')
                rating['severity_' + slug] = severity
                rating['prob_' + slug]     = prob
                rating['comment_' + slug]  = form.get('comment_' + slug, '').strip()
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
            behandlungsziel = form.get('behandlungsziel')
            if not behandlungsziel:
                return None, 'Bitte wählen Sie das Behandlungsziel aus.'
            rating['behandlungsziel'] = behandlungsziel
            status = form.get('status')
            topics = form.getlist('topics')
            if not status:
                return None, 'Bitte wählen Sie den Krankheitsstatus aus.'
            rating['status'] = status
            rating['topics'] = topics
            rating['diagnostik_types'] = form.getlist('diagnostik_types')
            rating['therapie_types'] = form.getlist('therapie_types')
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

    # All steps for this case done — next case or final questions
    if case_index + 1 < len(case_order):
        next_case = case_order[case_index + 1]
        return url_for('evaluate_step',
                       case_id=next_case, step_idx=0, tab_idx=0)

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
    if case_index > 0:
        prev_case = case_order[case_index - 1]
        prev_step = RATING_STEPS[-1]
        prev_tab  = 1 if prev_step['has_tabs'] else 0
        return url_for('evaluate_step',
                       case_id=prev_case, step_idx=len(RATING_STEPS) - 1, tab_idx=prev_tab)
    return url_for('intro')


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
        user_data['final_questions'] = fq
        user_data['completed']       = True
        user_data['completed_at']    = datetime.utcnow().isoformat()
        save_responses(username, user_data)
        _export_all_to_disk(username)
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


@app.route('/protocol-pdf')
@login_required
def protocol_pdf():
    """Serve the PDF for a specific case (via ?case_id=N) or fall back to first PDF found."""
    case_id = request.args.get('case_id', '')
    # Try to find the case-specific PDF in original_documents/
    if case_id and os.path.isdir(DOCUMENTS_DIR):
        for fname in os.listdir(DOCUMENTS_DIR):
            if not fname.lower().endswith('.pdf'):
                continue
            m = _re.match(r'[Ff]all(\d+)', fname)
            if m and m.group(1) == str(case_id):
                resp = send_file(os.path.join(DOCUMENTS_DIR, fname),
                                 mimetype='application/pdf')
                resp.headers['Content-Disposition'] = 'inline'
                return resp
    # Fallback: first PDF in original_documents/
    if os.path.isdir(DOCUMENTS_DIR):
        for fname in sorted(os.listdir(DOCUMENTS_DIR)):
            if fname.lower().endswith('.pdf'):
                resp = send_file(os.path.join(DOCUMENTS_DIR, fname),
                                 mimetype='application/pdf')
                resp.headers['Content-Disposition'] = 'inline'
                return resp
    return 'Kein Protokoll-PDF gefunden.', 404


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
        'experience_years', 'dermatology_years', 'role',
        'completed',
    ]
    writer.writerow(header)

    for username, data in sorted(responses.items()):
        assign_sum  = data.get('assignments_summary', {})
        assign_prob = data.get('assignments_problem', {})
        ratings     = data.get('ratings', {})
        case_order  = data.get('case_order', [])
        demo        = data.get('demographics', {})
        completed   = data.get('completed', False)

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
                        completed,
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
        'is_relevant', 'is_present', 'is_false',
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
                            is_relevant, is_present, is_false,
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
        completed   = data.get('completed', False)

        case_pos = {cid: pos for pos, cid in enumerate(case_order)}

        for case_id in sorted(ratings.keys(), key=lambda x: int(x) if x.isdigit() else x):
            case_ratings = ratings[case_id]
            case_texts   = text_dict.get(case_id, {})

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
                        'completed':            completed,
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


if __name__ == '__main__':
    app.run(debug=True)
