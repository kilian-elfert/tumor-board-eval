#!/usr/bin/env python3
"""
Create evaluator accounts interactively.

Usage:
    python setup_users.py

You will be prompted to enter one or more username/password pairs.
"""
import json
from werkzeug.security import generate_password_hash

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
        if len(password) >= 4:
            break
        print("  Password must be at least 4 characters. Try again.")

    role = input(f"  Role for '{username}' [evaluator/annotator/admin] (default: evaluator): ").strip().lower()
    if role not in ('evaluator', 'annotator', 'admin'):
        role = 'evaluator'

    entry = {"password": generate_password_hash(password)}
    if role != 'evaluator':
        entry["role"] = role
    users[username] = entry
    print(f"  Account '{username}' added (role: {role}).\n")

with open("users.json", "w") as f:
    json.dump(users, f, indent=2)

print("\nDone. Accounts written to users.json:")
for name in users:
    print(f"  {name}")
