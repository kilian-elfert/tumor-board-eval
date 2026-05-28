# Tumor Board Evaluation

A Flask web application for a clinical study evaluating LLM-generated tumor board case summaries and problem statements. Medical experts rate AI- vs. human-written texts on completeness, correctness, and clinical relevance across multiple melanoma cases.

---

## Overview

After login, evaluators run through a fixed sequence:

1. **Consent** form, **demographic** questionnaire, and a **study procedure** page.
2. A **case dashboard** ("Patientenakte") landing page lists all cases with progress per evaluator.

For each case the evaluator then steps through:

0. **Patient dashboard** â€” interactive anatomical body map, basic data, staging,
   molecular pathology, lab values, imaging diagnostics, therapies, and a
   clinical timeline. The original protocol PDF and per-modality imaging
   reports are linked from here.
1. **Information relevance.** Rate the clinical relevance of a fixed set of
   information items (`INFO_ITEMS` in `app.py`).
2. **Case summary (Zusammenfassung).** Evaluate the human-written and the
   LLM-generated version side by side across information content, false
   information, correctness, completeness, conciseness, post-editing effort,
   origin guess, and overall preference.
3. **Problem statement (Fragestellung).** Evaluate both versions on focus,
   guideline conformity, specificity, post-editing effort, origin guess,
   and preference.
4. **Final per-case questions** â€” case complexity rating and an optional
   free-text comment.

Which version appears as "A" or "B" is randomised per participant and
independently for the Zusammenfassung and the Fragestellung block, to prevent
order bias. Progress is persisted after every step in
`responses/responses_<username>.json`, so an interrupted session can be
resumed at the exact same step.

---

## Project Structure

```
app.py                     Main Flask application (routes, logic, data loading)
setup_users.py             Interactive script to create user accounts
                           (roles: evaluator / annotator / admin)
preprocess.py              Builds dashboard JSONs from source TK protocols
                           (uses the local OpenAI-compatible LLM endpoint)
pyproject.toml             Project metadata and dependencies (source of truth)
requirements.txt           Minimal pip fallback (prefer uv + pyproject.toml)
users.json                 Hashed credentials for all evaluator accounts
.env                       Local environment variables â€” not committed
                           (SECRET_KEY, NTFY_URL, DATA_ROOT, LLM_*)

texts_human/
  zusammenfassung/         Human-written case summaries (one .txt per case_id)
  fragestellung/           Human-written problem formulations

texts_llm/
  zusammenfassung/         LLM-generated case summaries
  fragestellung/           LLM-generated problem formulations

dashboard_data/            Pre-built patient dashboards: <case_id>_dashboard.json
                           (output of preprocess.py). Rendered as the
                           "Patientenakte" before each evaluation.

annotations/               Per-case researcher annotations: <case_id>.json
                           (status of each info item per text version:
                           enthalten / nicht_enthalten / falsch, plus the
                           corresponding text spans). Drives the false-info
                           highlighting and the missing/false/irrelevant
                           item lists shown on the Korrektheit,
                           Vollständigkeit, Prägnanz, and
                           Falschinformationen rating steps.

guideline/                 S3-Leitlinie PDF shown during the guideline-conformity step

responses/                 Per-evaluator response files: responses_<username>.json

exports/                   Auto-generated per-user exports (written on submission of
                           the final questions and on /export requests):
                             ratings_<user>.csv          Per-rating long-format CSV
                             ratings_<user>.json         Per-rating JSON records
                             integrity_items_<user>.csv  Per-info-item integrity /
                                                         false-info / missing-info
                             responses_raw_<user>.json   Verbatim copy of
                                                         responses_<user>.json

templates/                 Jinja2 HTML templates
  base.html                Base layout (navigation, stepper)
  _body_map.html           Reusable body map component (anatomical overview)
  case_dashboard.html      Patient dashboard shown before evaluation steps
  login.html               Login page
  consent.html             Informed consent
  demographics.html        Demographic questionnaire
  study_info.html          Study procedure overview
  intro.html               Welcome / start page
  dashboard.html           Per-user case-progress overview
  evaluate.html            Main rating step template
  final_questions.html     Final per-case questions
  complete.html            Case completion confirmation
  end.html                 Study completion page
  error.html               Error display

static/
  style.css                Application stylesheet
  dashboard.css            Dashboard-specific styles (body map, charts, timeline)
  body_map_echelon.png             Male body map image
  body_map_echelon_female.png      Female body map image
  body_map.jpg, certificate.jpg, signature.png,
  pdf-icon.png, wispermed.{png,svg}, Wispermed_Logo_de.png   UI assets
```

### External data root

Large case-source artefacts live outside the repo, under
`$DATA_ROOT/$SOURCES_SUBDIR/` (by default `â€¦/Data/sources/`). Files are named by
`case_id`, a 64-char SHA-256 stem of the source protocol:

```
<case_id>.pdf                       Original protocol PDF
<case_id>.txt                       Plain-text dump of the protocol
<case_id>_lab.txt                   Lab values (chronological)
<case_id>_verlaufsdoku.jsonl        Clinical course timeline (one JSON per line)
<case_id>_<modality>/...            Imaging reports (PDF + .txt) per modality,
                                    e.g. <case_id>_ct/, <case_id>_pet_ct/
```

`app.py` serves protocols and imaging directly from this directory. If
`DATA_ROOT` is unset, it falls back to in-repo `original_documents/` and
`imaging/` folders (legacy, not present in the current checkout).

---

## Setup

**Requirements:** Python â‰¥ 3.14, [uv](https://docs.astral.sh/uv/)

```bash
# Install dependencies
uv sync

# Create evaluator accounts
uv run setup_users.py
```

`setup_users.py` prompts for a username, password, and role (`evaluator`, `annotator`, or `admin`) for each account and writes hashed credentials to `users.json`.

| Role | Access |
|------|--------|
| `evaluator` (default) | Study rating flow |
| `annotator` | Annotation interface for marking spans as `enthalten` / `falsch` / `nicht_enthalten` |
| `admin` | Admin dashboard + export access |

---

## Running the App

```bash
uv run app.py
```

The app starts on `http://localhost:5001` (debug mode is on by default; disable
before exposing the app publicly).

### Environment variables

Loaded from `.env` (via `python-dotenv`) or the process environment:

| Variable | Purpose |
|----------|---------|
| `SECRET_KEY` | Flask session signing key. **Set this in production** â€” the built-in fallback is insecure. |
| `NTFY_URL`   | Optional [ntfy.sh](https://ntfy.sh) topic URL for push notifications when an evaluator starts a case. No-op if unset. |
| `LLM_BASE_URL` | Base URL of the OpenAI-compatible LLM endpoint used by `preprocess.py`. Defaults to `http://10.99.0.230:8004/v1`. |
| `LLM_MODEL` | Model id passed to the LLM endpoint (e.g. `openai/gpt-oss-120b`). |
| `LLM_API_KEY` | Optional bearer token for the LLM endpoint. Leave empty for the local server. |
| `DATA_ROOT`, `SOURCES_SUBDIR`, `OUTPUTS_SUBDIR`, `CSV_FILENAME` | External data paths. `preprocess.py` reads source files (`<case_id>.{txt,pdf}`, `<case_id>_lab.txt`, `<case_id>_verlaufsdoku.jsonl`, `<case_id>_<modality>/`) from `$DATA_ROOT/$SOURCES_SUBDIR/`; `app.py` serves protocol PDFs and imaging from there as well. |

```bash
SECRET_KEY=your-secret-key uv run app.py
```

---

## Data Flow

1. **Case discovery.** At startup `app.py` scans `texts_human/`, `texts_llm/`,
   `dashboard_data/`, and the configured sources directory and assembles the
   union of `case_id` stems (64-char SHA-256 hashes) found there.
2. **Texts.** Human and LLM versions of the case summary and problem statement
   are read from `texts_human/<kind>/<case_id>.txt` and
   `texts_llm/<kind>/<case_id>.txt`.
3. **Patient dashboards.** `dashboard_data/<case_id>_dashboard.json` is loaded
   per case and rendered as the "Patientenakte" (basic data, body map with
   metastases, therapies, lab values, imaging, comorbidities, staging,
   molecular pathology). Lab values, imaging entries, metastases and therapies
   are sorted chronologically at load time.
4. **Annotations.** `annotations/<case_id>.json` holds the researcher
   verdict for each information item per text version (`enthalten`,
   `nicht_enthalten`, `falsch`) plus the matching text spans. These drive
   the inline `falsch` highlighting on the Falschinformationen and
   Korrektheit steps and the displayed item lists on the Korrektheit,
   Vollständigkeit, and Prägnanz steps. Without this file those steps
   show no highlights/lists but still function.
4. **Imaging reports.** PDF and free-text reports are served on demand from
   `<case_id>_<modality>/` under the sources directory via `/api/imaging-pdf`
   and `/api/imaging-txt`.
5. **Protocol PDF.** `<case_id>.pdf` from the sources directory is served via
   `/protocol-pdf?case_id=<case_id>`.
6. **Guideline.** The S3-Leitlinie PDF in `guideline/` is served to evaluators
   during the guideline-conformity step.
7. **Responses.** Per-user state is persisted to
   `responses/responses_<username>.json` after every step.
8. **Exports.** When the final questions are submitted (and on `/export`),
   four files are written to `exports/` for the calling user; `/export` also
   returns `ratings_<user>.csv` as a download.
9. **Notifications.** If `NTFY_URL` is set, a push notification is sent the
   first time an evaluator opens a case dashboard.

---

## Generating dashboard data

### Patient dashboards (`preprocess.py`)

```bash
uv run preprocess.py
```

Reads `<case_id>.txt`, `<case_id>_lab.txt`, `<case_id>_verlaufsdoku.jsonl` and
the imaging report folders from `$DATA_ROOT/$SOURCES_SUBDIR/`, calls the LLM
endpoint configured via `LLM_BASE_URL` / `LLM_MODEL` to extract structured
clinical data, and writes one `dashboard_data/<case_id>_dashboard.json` per case.

---

## Dependencies

Declared in `pyproject.toml` (use `uv sync`):

| Package | Purpose |
|---------|---------|
| `flask >= 3.0` | Web framework |
| `werkzeug >= 3.0` | Password hashing, request utilities |
| `python-dotenv >= 1.0` | Loads `.env` at startup |
| `httpx >= 0.27` | HTTP client used by `preprocess.py` for the LLM endpoint |
| `tiktoken >= 0.12` | Token counting in helper scripts |
| `pypdf >= 6.10` | PDF parsing in `preprocess.py` |
| `dnspython >= 2.8` | Transitive utility |

`requirements.txt` is a minimal pip fallback and may lag behind
`pyproject.toml`; prefer `uv sync`.


---

## Sharing the project with a collaborator

Code lives in git; **patient data must never enter git**. Already excluded by
`.gitignore`: `.env`, `responses/`, `dashboard_data/`, `texts_human/`,
`texts_llm/`, `original_documents/`, `highlight_mappings.json`,
`annotations/`, `users.json`, `exports/`.

### What goes via git

The full repo (code, templates, static assets, `pyproject.toml`,
`requirements.txt`, this README, the public S3 guideline text, and
`.env.example`).

Before pushing:

1. Rotate any secrets that ever sat in `.env` and remove them from history if
   needed (`git log --all -- .env` / `git filter-repo`).
2. Confirm no patient files were ever committed:
   ```powershell
   git log --all --full-history -- dashboard_data texts_human texts_llm responses users.json highlight_mappings.json annotations
   ```


### Internal file sharing

Two bundles need to be transferred separately:

**Bundle A — app data (belongs *inside* the cloned repo)**

These folders/files live next to `app.py` and are read directly by the Flask
app. After cloning the repo, place each item at the path on the right (paths
are relative to the repo root):

| In the file-share bundle  | Place at (relative to repo root) | Required? |
|---------------------------|----------------------------------|-----------|
| `dashboard_data/`         | `./dashboard_data/`              | yes       |
| `texts_human/`            | `./texts_human/`                 | yes       |
| `texts_llm/`              | `./texts_llm/`                   | yes       |
| `annotations/`            | `./annotations/`                 | optional (drives `falsch` highlights and the item lists on Korrektheit / Vollständigkeit / Prägnanz / Falschinformationen; without it those steps show no highlights or lists) |
| `highlight_mappings.json` | `./highlight_mappings.json`      | optional (without it the Falschinformationen step falls back to bold headers) |

After unpacking, the repo root should look like:

```
tumor_board_eval/
├── app.py
├── dashboard_data/                 ← from file-share
│   ├── 1119efbd…_dashboard.json
│   └── …
├── texts_human/
│   ├── fragestellung/              ← from file-share
│   └── zusammenfassung/            ← from file-share
├── texts_llm/
│   ├── fragestellung/              ← from file-share
│   └── zusammenfassung/            ← from file-share├── annotations/                    ← optional, from file-share
│   ├── 1119efbd….json
│   └── …└── highlight_mappings.json         ← optional, from file-share
```

**Bundle B — raw source data / CSV (lives *outside* the repo)**

This is the larger sensitive dataset. Pick any local folder (e.g.
`C:\Users\<yourfolder>\Data`) and unpack the bundle so the layout is:

```
<DATA_ROOT>/
├── sampled_cases.csv
└── sources/
    ├── <case_id_1>/
    │   ├── *.pdf
    │   └── …
    └── <case_id_2>/
        └── …
```

Then set `DATA_ROOT` in `.env` to the **absolute path** of `<DATA_ROOT>` —
that is the parent of `sources/`, *not* `sources/` itself. Keep the defaults
`SOURCES_SUBDIR=sources` and `CSV_FILENAME=sampled_cases.csv` unless the
bundle uses different names.

### Setup steps for the collaborator

```powershell
git clone <repo-url>
cd tumor_board_eval
uv sync

# 1. Configure environment
Copy-Item .env.example .env
#   - set SECRET_KEY (e.g. python -c "import secrets; print(secrets.token_urlsafe(48))")
#   - set DATA_ROOT to the absolute path of the folder that contains
#     sources/ and sampled_cases.csv (Bundle B)

# 2. Unpack Bundle A into the repo root so dashboard_data/, texts_human/,
#    texts_llm/, annotations/ (and optional highlight_mappings.json) sit
#    next to app.py.

# 3. Unpack Bundle B at <DATA_ROOT> so <DATA_ROOT>\sources\ and
#    <DATA_ROOT>\sampled_cases.csv exist.

# 4. Create local evaluator account(s)
uv run setup_users.py

# 5. Run
uv run app.py  
```
