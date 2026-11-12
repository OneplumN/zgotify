#!/usr/bin/env python3
"""Create credentials only for a new, isolated Gotify fixture."""
import json
import os
from pathlib import Path
import secrets

root = Path(__file__).resolve().parent
directory = root / "secrets"
if not directory.is_dir() or directory.is_symlink():
    raise SystemExit("existing protected test secrets directory required")
paths = [directory / "gotify_fixture_password", directory / "gotify_fixture_config.json"]
if any(path.exists() or path.is_symlink() for path in paths):
    raise SystemExit("fixture credentials already exist; refusing overwrite")
password = secrets.token_hex(24)
values = [password + "\n", json.dumps({
    "defaultuser": {"name": "admin", "pass": password},
    "registration": False,
    "database": {"dialect": "sqlite3", "connection": "data/gotify.db"},
})]
for path, value in zip(paths, values):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(value)
print("isolated Gotify credentials created; values not printed")
