# Tumor Board Evaluation

A Flask web application for a clinical study evaluating LLM-generated tumor board case summaries and problem statements. Medical experts rate AI- vs. human-written texts on completeness, correctness, and clinical relevance across multiple melanoma cases.

---

## Overview

Physicians log in and are guided through a structured, multi-step evaluation of multiple melanoma cases. For each case they:

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
pyproject.toml          Project metadata and dependencies
users.json              Hashed credentials for all evaluator accounts

texts_human/
  zusammenfassung/      Human-written case summaries (fall_1 … fall_5)
  fragestellung/        Human-written problem formulations (fall_1 … fall_5)

texts_llm/
  zusammenfassungen/    LLM-generated case summaries (fall_1 … fall_5)
  fragestellungen/      LLM-generated problem formulations (fall_1 … fall_5)

original_documents/     Source PDF case files (fall_1.pdf … fall_5.pdf)
                        The app auto-discovers cases from this folder.

responses/              Per-evaluator response files (responses_<username>.json)

exports/
  tumor_board_evaluation.json   All responses exported as JSON
  tumor_board_evaluation.csv    All responses exported as CSV

templates/              Jinja2 HTML templates
  base.html             Base layout (navigation, stepper)
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
2. Case discovery is driven by PDF files in `original_documents/` (named `fall_1.pdf`, `fall_2.pdf`, …).
3. Evaluator responses are saved per-user as `responses/responses_<username>.json`.
4. An admin can download all responses as JSON or CSV via the `/export` route.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `flask >= 3.0` | Web framework |
| `werkzeug >= 3.0` | Password hashing, request utilities |
