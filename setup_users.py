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

    users[username] = {"password": generate_password_hash(password)}
    print(f"  Account '{username}' added.\n")

with open("users.json", "w") as f:
    json.dump(users, f, indent=2)

print("\nDone. Accounts written to users.json:")
for name in users:
    print(f"  {name}")
