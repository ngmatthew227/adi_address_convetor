#!/usr/bin/env python3
"""
Import ADI MySQL dump (from zip) into a MySQL database.

This script:
  1. Optionally extracts the .zip containing the SQL dump files.
  2. Optionally runs create_schema.sql (drops & recreates the target schema).
  3. Parses and executes the large EADI100.sql dump statement-by-statement.

Configuration is read from config.json (or overridden via CLI arguments).
"""

import argparse
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

try:
    import pymysql
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "pymysql is required; please run: pip install pymysql"
    ) from exc

# ---------------------------------------------------------------------------
# Default configuration (overridden by config.json / CLI args)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "zip_path": r"data\raw\adi_version\ADI_MySQL_326.zip",
    "extract_dir": r"data\raw\adi_version\ADI_MySQL_326",
    "mysql": {
        "host": "127.0.0.1",
        "port": 3306,
        "user": "root",
        "password": "secretP@ssw0rd",
        "database": "EADI100",
        "charset": "utf8mb4",
    },
    "options": {
        "extract_zip": True,
        "run_create_schema": True,
        "drop_database_first": True,
        "max_allowed_packet_mb": 64,
    },
}

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
RAW_DIR = PROJECT_ROOT / "data" / "raw"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def load_config(config_path: Path) -> dict:
    """Load configuration from a JSON file, falling back to defaults."""
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy defaults
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as fh:
            user_cfg = json.load(fh)
        _deep_merge(config, user_cfg)
    return config


def _deep_merge(base: dict, override: dict) -> None:
    """Recursively merge override into base (in place)."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


# ---------------------------------------------------------------------------
# Zip extraction
# ---------------------------------------------------------------------------
def extract_zip(zip_path: Path, extract_dir: Path) -> None:
    """Extract all members of a zip archive into extract_dir."""
    if not zip_path.exists():
        raise FileNotFoundError(f"Zip file not found: {zip_path}")
    extract_dir.mkdir(parents=True, exist_ok=True)
    print(f"[1/3] Extracting {zip_path.name} -> {extract_dir}")
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            target = extract_dir / member.filename
            # Guard against path traversal
            if not str(target.resolve()).startswith(str(extract_dir.resolve())):
                raise RuntimeError(f"Unsafe path in zip: {member.filename}")
        zf.extractall(extract_dir)
    print(f"      Extracted {len(zf.infolist())} file(s).")


# ---------------------------------------------------------------------------
# SQL statement parser
# ---------------------------------------------------------------------------
def iter_sql_statements(file_path: Path):
    """
    Yield complete SQL statements from a MySQL dump file.

    Handles:
      - '--' line comments
      - non-executable '/* ... */' block comments (incl. multi-line)
      - executable '/*!...*/' comments (kept as statements)
      - multi-line statements (e.g. CREATE TABLE) terminated by ';'
    """
    buffer_lines: list[str] = []
    in_block_comment = False

    with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stripped = line.strip()

            if not stripped:
                continue

            # Skip '--' line comments
            if stripped.startswith("--"):
                continue

            # Handle non-executable block comments
            if stripped.startswith("/*") and not stripped.startswith("/*!"):
                if "*/" in stripped:
                    continue  # single-line comment
                in_block_comment = True
                continue

            if in_block_comment:
                if "*/" in stripped:
                    in_block_comment = False
                continue

            # Accumulate statement lines
            buffer_lines.append(line)

            # A statement is complete when a line ends with ';'
            if stripped.endswith(";"):
                statement = "".join(buffer_lines).strip()
                buffer_lines = []
                if statement:
                    yield statement


# ---------------------------------------------------------------------------
# MySQL execution
# ---------------------------------------------------------------------------
def connect_mysql(cfg: dict, database: str | None = None):
    """Create a PyMySQL connection from config.

    If ``database`` is None, the configured database name is used.
    Pass ``database=""`` to connect without selecting a database
    (useful before the target schema exists).
    """
    mysql = cfg["mysql"]
    return pymysql.connect(
        host=mysql["host"],
        port=int(mysql["port"]),
        user=mysql["user"],
        password=mysql["password"],
        database=database if database is not None else mysql["database"],
        charset=mysql.get("charset", "utf8mb4"),
        autocommit=False,
        local_infile=True,
    )


def run_sql_file(conn, file_path: Path, label: str) -> int:
    """Execute all statements in a SQL file. Returns statement count."""
    count = 0
    with conn.cursor() as cur:
        for statement in iter_sql_statements(file_path):
            try:
                cur.execute(statement)
            except pymysql.err.OperationalError as exc:
                # Tolerate "Can't drop database ... doesn't exist" (errno 1008)
                # so the schema file can run on a fresh server.
                if exc.args and exc.args[0] == 1008:
                    print(f"      Skipping (db does not exist): {statement}")
                    continue
                raise
            count += 1
    conn.commit()
    print(f"      Executed {count} statement(s) from {label}.")
    return count


def import_dump(conn, dump_path: Path) -> int:
    """Execute the main dump file with progress reporting."""
    total_lines = sum(1 for _ in open(dump_path, "r", encoding="utf-8", errors="replace"))
    print(f"[3/3] Importing {dump_path.name} ({total_lines:,} lines) ...")

    statement_count = 0
    start = time.time()
    with conn.cursor() as cur:
        for statement in iter_sql_statements(dump_path):
            cur.execute(statement)
            statement_count += 1
            if statement_count % 50 == 0:
                elapsed = time.time() - start
                print(f"      {statement_count} statements executed ({elapsed:.1f}s)")
    conn.commit()
    elapsed = time.time() - start
    print(f"      Done: {statement_count} statements in {elapsed:.1f}s.")
    return statement_count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="Import ADI MySQL dump (zip) into a MySQL database."
    )
    parser.add_argument("--config", type=Path, default=CONFIG_PATH,
                        help="Path to config JSON file.")
    parser.add_argument("--zip", type=str, help="Path to the zip file.")
    parser.add_argument("--extract-dir", type=str,
                        help="Directory to extract the zip into.")
    parser.add_argument("--host", type=str, help="MySQL host.")
    parser.add_argument("--port", type=int, help="MySQL port.")
    parser.add_argument("--user", type=str, help="MySQL user.")
    parser.add_argument("--password", type=str, help="MySQL password.")
    parser.add_argument("--database", type=str, help="MySQL database name.")
    parser.add_argument("--no-extract", action="store_true",
                        help="Skip zip extraction.")
    parser.add_argument("--no-schema", action="store_true",
                        help="Skip running create_schema.sql.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(Path(args.config))

    # Apply CLI overrides
    if args.zip:
        config["zip_path"] = args.zip
    if args.extract_dir:
        config["extract_dir"] = args.extract_dir
    if args.host:
        config["mysql"]["host"] = args.host
    if args.port:
        config["mysql"]["port"] = args.port
    if args.user:
        config["mysql"]["user"] = args.user
    if args.password:
        config["mysql"]["password"] = args.password
    if args.database:
        config["mysql"]["database"] = args.database
    if args.no_extract:
        config["options"]["extract_zip"] = False
    if args.no_schema:
        config["options"]["run_create_schema"] = False

    zip_path = Path(config["zip_path"])
    extract_dir = Path(config["extract_dir"])
    # Resolve relative paths against the project root so the script works
    # regardless of the current working directory.
    if not zip_path.is_absolute():
        zip_path = PROJECT_ROOT / zip_path
    if not extract_dir.is_absolute():
        extract_dir = PROJECT_ROOT / extract_dir
    options = config["options"]

    # 1. Extract zip
    if options.get("extract_zip", True):
        extract_zip(zip_path, extract_dir)
    else:
        print("[1/3] Skipping zip extraction (--no-extract).")

    # Locate SQL files in the extract dir
    schema_file = extract_dir / "create_schema.sql"
    dump_file = extract_dir / "EADI100.sql"
    if not dump_file.exists():
        # Fall back to any *.sql file that is not create_schema.sql
        candidates = [p for p in extract_dir.glob("*.sql")
                      if p.name.lower() != "create_schema.sql"]
        if not candidates:
            raise FileNotFoundError(
                f"No dump SQL file found in {extract_dir}"
            )
        dump_file = candidates[0]

    # 2. Connect to MySQL (without selecting a database yet, since the
    #    target schema may not exist until create_schema.sql runs).
    print("[2/3] Connecting to MySQL ...")
    conn = connect_mysql(config, database="")

    try:
        # Optionally drop/recreate schema first
        if options.get("run_create_schema", True):
            if not schema_file.exists():
                print("      WARNING: create_schema.sql not found; skipping.")
            else:
                print(f"      Running {schema_file.name} ...")
                run_sql_file(conn, schema_file, schema_file.name)
        else:
            print("      Skipping create_schema.sql (--no-schema).")

        # Select the target database now that the schema exists
        db_name = config["mysql"]["database"]
        with conn.cursor() as cur:
            cur.execute(f"USE `{db_name}`")

        # 3. Import the dump
        import_dump(conn, dump_file)

    finally:
        conn.close()

    print("\nImport completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())