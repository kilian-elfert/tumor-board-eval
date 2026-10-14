#!/usr/bin/env python3
"""
Create evaluator / annotator / admin accounts, interactively or from a file.

Usage:
    python setup_users.py                 # interactive prompts
    python setup_users.py accounts.txt    # batch mode from a comma-separated file

File format (one account per line):

    username, role, password

  - role is one of: evaluator, annotator, admin (an empty role -> evaluator)
  - password must be at least 4 characters long
  - blank lines and lines starting with '#' are ignored
  - fields are split on the first two commas, so the password (last field) may
    itself contain commas; surrounding whitespace around each field is trimmed

The accounts are written to users.json, overwriting any existing file (same
behaviour as the interactive mode). For the default 'evaluator' role no explicit
"role" key is stored, which the app treats as evaluator.

Examples (accounts.txt):
    # study participants
    alice,   evaluator, secret123
    bob,     admin,     adminpw1
    carla,   ,          needsreview   # empty role -> evaluator
"""
import json
import sys

from werkzeug.security import generate_password_hash

VALID_ROLES = ('evaluator', 'annotator', 'admin')
MIN_PASSWORD_LEN = 4


def make_entry(password, role):
    """Build a users.json entry. 'evaluator' is the implicit default (no role key)."""
    entry = {"password": generate_password_hash(password)}
    if role != 'evaluator':
        entry["role"] = role
    return entry


def parse_file(path):
    """Parse a comma-separated accounts file into a users dict.

    Returns (users, skipped) where skipped is the number of invalid/duplicate
    lines that were ignored.
    """
    users = {}
    skipped = 0
    with open(path, 'r', encoding='utf-8-sig') as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith('#'):
                continue

            parts = line.split(',', 2)
            if len(parts) < 3:
                print(f"  [Zeile {lineno}] Übersprungen: erwartet 'username,role,password' "
                      f"-> {raw.rstrip()}")
                skipped += 1
                continue

            username = parts[0].strip()
            role     = parts[1].strip().lower()
            password = parts[2].strip()

            if not username:
                print(f"  [Zeile {lineno}] Übersprungen: leerer Benutzername.")
                skipped += 1
                continue
            if username in users:
                print(f"  [Zeile {lineno}] '{username}' bereits vorhanden — übersprungen.")
                skipped += 1
                continue
            if len(password) < MIN_PASSWORD_LEN:
                print(f"  [Zeile {lineno}] '{username}' übersprungen: "
                      f"Passwort muss mindestens {MIN_PASSWORD_LEN} Zeichen haben.")
                skipped += 1
                continue
            if role and role not in VALID_ROLES:
                print(f"  [Zeile {lineno}] '{username}': unbekannte Rolle '{role}' "
                      f"-> 'evaluator'.")
                role = 'evaluator'
            if not role:
                role = 'evaluator'

            users[username] = make_entry(password, role)
            print(f"  Account '{username}' hinzugefügt (role: {role}).")
    return users, skipped


def interactive():
    """Prompt for accounts one by one (original behaviour)."""
    users = {}
    print("=== Tumor Board Evaluator Account Setup ===")
    print("Enter accounts for study participants. Leave username blank to finish.\n")

    while True:
        username = input("Username (or press Enter to finish): ").strip()
        if not username:
            if not users:
                print("No accounts entered. Exiting.")
                raise SystemExit(1)
            break
        if username in users:
            print(f"  '{username}' already added — skipping.")
            continue

        while True:
            password = input(f"  Password for '{username}': ").strip()
            if len(password) >= MIN_PASSWORD_LEN:
                break
            print(f"  Password must be at least {MIN_PASSWORD_LEN} characters. Try again.")

        role = input(
            f"  Role for '{username}' [evaluator/annotator/admin] (default: evaluator): "
        ).strip().lower()
        if role not in VALID_ROLES:
            role = 'evaluator'

        users[username] = make_entry(password, role)
        print(f"  Account '{username}' added (role: {role}).\n")
    return users


def main(argv):
    if argv:
        path = argv[0]
        print(f"=== Tumor Board Evaluator Account Setup (Datei: {path}) ===\n")
        try:
            users, skipped = parse_file(path)
        except OSError as e:
            print(f"Fehler: Datei kann nicht gelesen werden: {e}")
            raise SystemExit(1)
        if not users:
            print("\nKeine gültigen Accounts gefunden. Abbruch (users.json nicht geändert).")
            raise SystemExit(1)
        if skipped:
            print(f"\nHinweis: {skipped} Zeile(n) übersprungen.")
    else:
        users = interactive()

    with open("users.json", "w", encoding='utf-8') as f:
        json.dump(users, f, indent=2)

    print("\nDone. Accounts written to users.json:")
    for name in users:
        role = users[name].get('role', 'evaluator')
        print(f"  {name} ({role})")


if __name__ == '__main__':
    main(sys.argv[1:])
