#!/usr/bin/env python3
"""Verify congruence of dashboard JSON files with their source data.

For each of the 5 cases in dashboard_data/, this script compares the
dashboard fields against:
  - sampled_cases.csv row (demographics)
  - {case_id}.txt protocol (LLM-extracted clinical fields)
  - {case_id}_verlaufsdoku.jsonl (timeline)
  - {case_id}_lab.txt (lab values)
  - {case_id}_{ct,mrt,sono,pet_ct} dirs (imaging)

Reports issues at three severities: ERROR (clear mismatch), WARN
(possible issue / no evidence found), OK (consistent).
"""

import csv
import json
import os
import re
import unicodedata
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(override=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_DIR = os.path.join(BASE_DIR, 'dashboard_data')
DATA_ROOT = os.path.expanduser(os.environ.get('DATA_ROOT', ''))
SOURCES_DIR = os.path.join(DATA_ROOT, os.environ.get('SOURCES_SUBDIR', 'sources'))
CSV_PATH = os.path.join(DATA_ROOT, os.environ.get('CSV_FILENAME', 'sampled_cases.csv'))


# ── helpers ──────────────────────────────────────────────────────────────────

def _norm(s):
    """Lowercase, strip accents, collapse whitespace."""
    if s is None:
        return ''
    s = str(s)
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r'\s+', ' ', s).strip().lower()
    return s


def _read(path):
    if not os.path.isfile(path):
        return ''
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def _load_jsonl(path):
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _calc_age(birth, ref):
    try:
        bd = datetime.strptime(birth, '%Y-%m-%d')
        rd = datetime.strptime(ref.strip().split(' ')[0], '%Y-%m-%d')
        return rd.year - bd.year - ((rd.month, rd.day) < (bd.month, bd.day))
    except Exception:
        return None


def _fmt_date(s):
    if not s:
        return ''
    try:
        return datetime.strptime(s, '%Y-%m-%d').strftime('%d.%m.%Y')
    except Exception:
        return s


# ── per-case verification ────────────────────────────────────────────────────

class Report:
    def __init__(self, case_id):
        self.case_id = case_id
        self.errors = []
        self.warnings = []
        self.oks = []

    def err(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def ok(self, msg):
        self.oks.append(msg)


def verify_demographics(d, row, rep):
    if row is None:
        rep.warn("No CSV row found for this case – cannot verify demographics.")
        return
    # name
    fam = row.get('pn0.family', '').strip()
    if fam and _norm(fam) != _norm(d.get('name', '')):
        rep.err(f"name mismatch: dashboard={d.get('name')!r} CSV.pn0.family={fam!r}")
    elif fam:
        rep.ok(f"name matches CSV ({fam})")
    # dob
    csv_dob = _fmt_date(row.get('p0.birthDate', ''))
    if csv_dob and csv_dob != d.get('dob', ''):
        rep.err(f"dob mismatch: dashboard={d.get('dob')!r} CSV={csv_dob!r}")
    elif csv_dob:
        rep.ok(f"dob matches CSV ({csv_dob})")
    # age
    age = _calc_age(row.get('p0.birthDate', ''), row.get('d0_issued', ''))
    if age is not None and d.get('age') != age:
        rep.err(f"age mismatch: dashboard={d.get('age')!r} computed={age}")
    elif age is not None:
        rep.ok(f"age matches computed value ({age})")
    # sex
    gender_raw = row.get('p0.gender', '').lower()
    gmap = {'male': 'M', 'female': 'W', 'm': 'M', 'f': 'W',
            'männlich': 'M', 'weiblich': 'W'}
    expected_sex = gmap.get(gender_raw, '')
    actual_sex = d.get('sex', '')
    if expected_sex and expected_sex != actual_sex:
        rep.err(f"sex mismatch: dashboard={actual_sex!r} CSV.gender={gender_raw!r} → expected {expected_sex!r}")
    elif not expected_sex and not actual_sex:
        rep.warn(f"sex empty in dashboard; CSV.gender={gender_raw!r} not mappable")
    else:
        rep.ok(f"sex={actual_sex!r}")
    # pat_id
    csv_pid = str(row.get('pi0.value', '')).strip()
    if csv_pid and csv_pid != str(d.get('pat_id', '')).strip():
        rep.err(f"pat_id mismatch: dashboard={d.get('pat_id')!r} CSV={csv_pid!r}")
    elif csv_pid:
        rep.ok(f"pat_id matches CSV ({csv_pid})")


def verify_timeline(d, jsonl_entries, rep):
    dashboard_tl = d.get('timeline', [])
    src_n = len(jsonl_entries)
    db_n = len(dashboard_tl)
    if db_n > src_n:
        rep.err(f"timeline: dashboard has {db_n} entries, source JSONL only {src_n}")
    elif db_n < src_n:
        # Some entries are filtered as boilerplate – allow but report
        rep.ok(f"timeline: {db_n}/{src_n} kept (rest filtered as boilerplate)")
    else:
        rep.ok(f"timeline: all {src_n} entries kept")

    # Verify each timeline entry's date+event substring is present in source
    src_texts = []
    for e in jsonl_entries:
        text = e.get('text', e.get('title', ''))
        src_texts.append(_norm(text)[:200])
    src_blob = '\n'.join(src_texts)

    missing = 0
    for entry in dashboard_tl:
        ev = _norm(entry.get('cleaned_event', entry.get('event', '')))[:80]
        if ev and ev not in src_blob:
            missing += 1
    if missing:
        rep.err(f"timeline: {missing}/{db_n} entries' text not found in source JSONL")


def verify_lab(d, lab_text, rep):
    db_labs = d.get('lab_values', [])
    # Count parseable lines in source
    src_n = 0
    line_re = re.compile(r'^[^:]+:\s*\S+.*Date:\s*\S+')
    for line in lab_text.splitlines():
        line = line.strip()
        if line and not line.startswith('[') and line_re.match(line):
            src_n += 1
    if src_n == len(db_labs):
        rep.ok(f"lab values: {len(db_labs)} entries match source line count")
    else:
        rep.warn(f"lab values: dashboard {len(db_labs)} vs source {src_n} parseable lines")

    # Spot-check first/last value present in source
    if db_labs:
        for lab in (db_labs[0], db_labs[-1]):
            marker = lab.get('marker', '')
            value = lab.get('value', '')
            if marker and value and not (marker in lab_text and value in lab_text):
                rep.warn(f"lab: marker/value not literally found in lab.txt: {marker}={value}")


def verify_imaging(d, case_id, rep):
    db_img = d.get('imaging', [])
    src_n = 0
    for suffix in ('_ct', '_pet_ct', '_mrt', '_sono'):
        dir_path = os.path.join(SOURCES_DIR, f'{case_id}{suffix}')
        if os.path.isdir(dir_path):
            txts = [f for f in os.listdir(dir_path) if f.endswith(('.txt', '.pdf'))]
            bases = set(f.rsplit('.', 1)[0] for f in txts)
            src_n += len(bases)
    if src_n == len(db_img):
        rep.ok(f"imaging: {len(db_img)} entries match source dirs")
    else:
        rep.warn(f"imaging: dashboard {len(db_img)} vs source {src_n} files")


def _evidence(text_norm, *substrs):
    """True if any of the normalised substrings appears in text_norm."""
    for s in substrs:
        if s and s in text_norm:
            return True
    return False


def verify_clinical(d, protocol, rep):
    """Check LLM-extracted clinical fields against the protocol text."""
    p = _norm(protocol)

    # primary_diagnosis – usually contains 'melanom'
    pdiag = d.get('primary_diagnosis', '')
    if pdiag:
        # extract main word(s)
        keys = [k for k in re.split(r'[\s,/\-]', _norm(pdiag)) if len(k) > 4]
        if keys and not any(k in p for k in keys):
            rep.warn(f"primary_diagnosis '{pdiag}' – no token found in protocol")
        else:
            rep.ok(f"primary_diagnosis '{pdiag}' – evidence found")

    # primary_location
    ploc = d.get('primary_location', '')
    if ploc and _norm(ploc) not in p:
        # try first word
        first = _norm(ploc).split(' ')[0]
        if first and first not in p:
            rep.warn(f"primary_location '{ploc}' – not found in protocol")

    # tumor_thickness e.g. "1.4 mm"
    tt = d.get('tumor_thickness', '')
    if tt:
        m = re.search(r'(\d+[.,]?\d*)\s*mm', tt)
        if m:
            num = m.group(1).replace('.', ',')
            num_dot = m.group(1).replace(',', '.')
            if num not in protocol and num_dot not in protocol:
                rep.warn(f"tumor_thickness '{tt}' – numeric value not in protocol")
            else:
                rep.ok(f"tumor_thickness '{tt}' – numeric evidence found")

    # diagnosis_date
    dd = d.get('diagnosis_date', '')
    if dd:
        # Look for year and either day/month or full date
        try:
            dt = datetime.strptime(dd, '%d.%m.%Y')
            year = str(dt.year)
            mmYY = dt.strftime('%m/%Y')
            mmYY2 = dt.strftime('%m.%Y')
            if dd in protocol or mmYY in protocol or mmYY2 in protocol or year in protocol:
                rep.ok(f"diagnosis_date '{dd}' – evidence in protocol")
            else:
                rep.warn(f"diagnosis_date '{dd}' – not found in protocol")
        except Exception:
            pass

    # resection_status (R0/R1/Rx)
    rs = d.get('resection_status', '')
    if rs:
        if _norm(rs) in p or rs in protocol:
            rep.ok(f"resection_status '{rs}' – evidence in protocol")
        else:
            rep.warn(f"resection_status '{rs}' – not found in protocol")

    # mutations
    muts = d.get('mutations', {}) or {}
    for gene, status in muts.items():
        if gene and _norm(gene) not in p:
            rep.warn(f"mutation gene '{gene}' – not in protocol")
        else:
            rep.ok(f"mutation gene '{gene}' = {status!r} – evidence in protocol")

    # initial_staging T/N/M strings should be present in protocol
    for label in ('initial_staging', 'staging'):
        st = d.get(label, {}) or {}
        for key in ('t', 'n', 'm', 'uicc'):
            v = st.get(key, '')
            if not v:
                continue
            # uicc values like "Stadium IV"
            if _norm(v) in p or v in protocol:
                rep.ok(f"{label}.{key}={v!r} – evidence in protocol")
            else:
                rep.warn(f"{label}.{key}={v!r} – NOT in protocol text")

    # comorbidities – each should have at least one keyword in protocol
    for c in d.get('comorbidities', []):
        cn = _norm(c)
        keywords = [w for w in re.split(r'[\s,()/\-]', cn) if len(w) > 4]
        if keywords and not any(k in p for k in keywords):
            rep.warn(f"comorbidity '{c}' – no keyword found in protocol")

    # ECOG
    ecog = d.get('ecog', '')
    if ecog:
        m = re.match(r'(\d)', ecog)
        if m and f"ecog: {m.group(1)}" in p:
            rep.ok(f"ECOG '{ecog}' – evidence in protocol")
        elif m and ('ecog' in p):
            rep.warn(f"ECOG value '{ecog}' – ECOG mentioned but exact value not matched")

    # PD-L1
    pdl1 = d.get('pdl1', '')
    if pdl1:
        if 'pd-l1' in p or 'pdl1' in p or 'tps' in p:
            rep.ok(f"PD-L1 '{pdl1}' – evidence in protocol")
        else:
            rep.warn(f"PD-L1 '{pdl1}' – no PD-L1/TPS mention in protocol")

    # name should appear in protocol
    name = d.get('name', '')
    if name and _norm(name) not in p:
        # Sometimes names are pseudonymised; only error if csv had the name
        rep.warn(f"name '{name}' – not found in protocol text")

    # primary_tumor_status / metastases consistency
    mets_body = d.get('metastases_body', [])
    mets_detail = d.get('metastases_detail', [])
    detail_regions = sorted({m.get('region', '') for m in mets_detail if m.get('region')})
    body_set = sorted(set(mets_body))
    if body_set != detail_regions:
        rep.err(f"metastases_body {body_set} ≠ regions in metastases_detail {detail_regions}")
    else:
        rep.ok(f"metastases_body and metastases_detail regions consistent ({len(body_set)} regions)")


# ── main ─────────────────────────────────────────────────────────────────────

def load_csv_rows():
    rows = {}
    if not os.path.isfile(CSV_PATH):
        return rows
    with open(CSV_PATH, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows[r.get('d0.id', '')] = r
    return rows


def main():
    csv_rows = load_csv_rows()
    files = sorted(f for f in os.listdir(DASHBOARD_DIR) if f.endswith('_dashboard.json'))
    print(f"Found {len(files)} dashboard files. Sources dir: {SOURCES_DIR}\n")

    summary = []

    for fname in files:
        case_id = fname.replace('_dashboard.json', '')
        path = os.path.join(DASHBOARD_DIR, fname)
        with open(path, 'r', encoding='utf-8') as f:
            d = json.load(f)
        rep = Report(case_id)

        protocol = _read(os.path.join(SOURCES_DIR, f'{case_id}.txt'))
        lab_text = _read(os.path.join(SOURCES_DIR, f'{case_id}_lab.txt'))
        jsonl = _load_jsonl(os.path.join(SOURCES_DIR, f'{case_id}_verlaufsdoku.jsonl'))

        if not protocol:
            rep.err(f"protocol text {case_id}.txt missing")

        verify_demographics(d, csv_rows.get(case_id), rep)
        verify_timeline(d, jsonl, rep)
        verify_lab(d, lab_text, rep)
        verify_imaging(d, case_id, rep)
        if protocol:
            verify_clinical(d, protocol, rep)

        # Print
        print('=' * 80)
        print(f"CASE {case_id[:12]}…  ({d.get('name', '?')})")
        print('=' * 80)
        if rep.errors:
            print(f"  ERRORS ({len(rep.errors)}):")
            for e in rep.errors:
                print(f"    ✗ {e}")
        if rep.warnings:
            print(f"  WARNINGS ({len(rep.warnings)}):")
            for w in rep.warnings:
                print(f"    ! {w}")
        print(f"  OK checks: {len(rep.oks)}")
        print()
        summary.append((case_id[:12], len(rep.errors), len(rep.warnings), len(rep.oks)))

    print('=' * 80)
    print("SUMMARY")
    print('=' * 80)
    print(f"{'case':<14} {'errors':>8} {'warnings':>10} {'ok':>6}")
    for cid, e, w, ok in summary:
        print(f"{cid:<14} {e:>8} {w:>10} {ok:>6}")


if __name__ == '__main__':
    main()
