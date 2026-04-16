# Tumor Board Evaluation

A Flask web application for a clinical study evaluating LLM-generated tumor board case summaries and problem statements. Medical experts rate AI- vs. human-written texts on completeness, correctness, and clinical relevance across multiple melanoma cases.

---

## Overview

Physicians log in and are guided through a structured, multi-step evaluation of multiple melanoma cases. For each case they:

0. Review a **patient dashboard** with an interactive anatomical body map, staging, molecular pathology, lab values, imaging diagnostics, and clinical timeline.
1. Rate the **clinical relevance** of a defined set of information items.
2. Evaluate both a human-written and an LLM-generated **case summary** across several dimensions (information content, false information, correctness, completeness, conciseness, post-editing effort, origin guess, and overall preference).
3. Evaluate both versions of the **problem statement** (focus, guideline conformity, specificity, post-editing effort, origin guess, and preference).
4. Provide a final **case complexity** rating and optional comment.

Text assignment (which version appears as "A" or "B") is randomised per participant and independently per block (case summary and problem statement each get their own counterbalanced assignment) to prevent order bias.

---

## Project Structure

```
app.py                  Main Flask application (routes, logic, data loading)
setup_users.py          Interactive script to create evaluator accounts
generate_highlights.py  Generates highlight_mappings.json via OpenAI API
highlight_mappings.json Pre-computed text excerpt → info-category mappings
pyproject.toml          Project metadata and dependencies
users.json              Hashed credentials for all evaluator accounts

texts_human/
  zusammenfassung/      Human-written case summaries (fall_1 … fall_5)
  fragestellung/        Human-written problem formulations (fall_1 … fall_5)

texts_llm/
  zusammenfassungen/    LLM-generated case summaries (fall_1 … fall_5)
  fragestellungen/      LLM-generated problem formulations (fall_1 … fall_5)

original_documents/     Case source files (Fall<N>_<hash>.txt)
                        The app auto-discovers cases from filenames.

guideline/              S3-Leitlinie PDF shown during guideline-conformity rating

responses/              Per-evaluator response files (responses_<username>.json)

exports/
  tumor_board_evaluation.json   All responses exported as JSON
  tumor_board_evaluation.csv    All responses exported as CSV

templates/              Jinja2 HTML templates
  base.html             Base layout (navigation, stepper)
  _body_map.html        Reusable body map component (anatomical overview)
  case_dashboard.html   Patient dashboard shown before evaluation steps
  login.html            Login page
  consent.html          Informed consent
  demographics.html     Demographic questionnaire
  study_info.html       Study procedure overview
  intro.html            Per-case introduction / PDF viewer
  evaluate.html         Main rating step template
  final_questions.html  Final case questions
  complete.html         Case completion confirmation
  end.html              Study completion page
  error.html            Error display

static/
  style.css             Application stylesheet
  dashboard.css         Dashboard-specific styles (body map, charts, timeline)
  body_map_echelon.png  Male body map image
  body_map_echelon_female.png  Female body map image
```

---

## Setup

**Requirements:** Python ≥ 3.14, [uv](https://docs.astral.sh/uv/)

```bash
# Install dependencies
uv sync

# Create evaluator accounts
uv run setup_users.py
```

`setup_users.py` prompts for username/password pairs and writes hashed credentials to `users.json`.

### Generate highlight mappings (optional)

Highlight mappings power the colour-coded text annotations during evaluation. To generate or regenerate them:

```bash
export OPENAI_API_KEY=sk-...
uv run generate_highlights.py          # all cases
uv run generate_highlights.py --case 3  # single case
uv run generate_highlights.py --dry-run # preview without API calls
```

The script writes `highlight_mappings.json`. It runs incrementally — existing cases are skipped unless `--case` is specified.

---

## Running the App

```bash
uv run app.py
```

The app starts on `http://localhost:5000` by default.

For production, set the `SECRET_KEY` environment variable:

```bash
SECRET_KEY=your-secret-key uv run app.py
```

---

## Data Flow

1. The app reads case content from `texts_human/` and `texts_llm/` at startup.
2. Case discovery is driven by `.txt` files in `original_documents/` matching the pattern `Fall<N>_*.txt`.
3. `highlight_mappings.json` (if present) is loaded to annotate text excerpts during evaluation.
4. The guideline PDF in `guideline/` is served to evaluators during the guideline-conformity step.
5. Evaluator responses are saved per-user as `responses/responses_<username>.json`.
6. The `responses/` and `exports/` directories are created automatically when needed.
7. An admin can download all responses as JSON or CSV via the `/export` route.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `flask >= 3.0` | Web framework |
| `werkzeug >= 3.0` | Password hashing, request utilities |
