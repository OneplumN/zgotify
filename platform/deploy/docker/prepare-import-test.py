#!/usr/bin/env python3
"""Provisioning material for a NEW disposable MySQL import-test schema only."""
import os
from pathlib import Path
import secrets

directory = Path(__file__).resolve().parent / "secrets"
if not directory.is_dir() or directory.is_symlink():
    raise SystemExit("protected test secrets directory required")
paths = [directory / "mysql_import_test_dsn", directory / "init-import-test.sql"]
if any(path.exists() or path.is_symlink() for path in paths):
    raise SystemExit("import fixture already prepared; refusing overwrite")
password = secrets.token_hex(32)
values = [
    "zgotify_import_it:" + password + "@tcp(database:3306)/zgotify_test_import_v2\n",
    "CREATE DATABASE zgotify_test_import_v2 CHARACTER SET utf8mb4;\n"
    "CREATE USER 'zgotify_import_it'@'%' IDENTIFIED BY '" + password + "';\n"
    "GRANT ALL PRIVILEGES ON `zgotify\\_test\\_import\\_v2`.* TO 'zgotify_import_it'@'%';\n",
]
for path, value in zip(paths, values):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(value)
os.chmod(paths[0], 0o444)
print("import fixture provisioning material created; credentials not printed")
