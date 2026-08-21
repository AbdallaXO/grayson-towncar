#!/usr/bin/env python
"""Pull a fresh production snapshot from Railway Postgres into content/db.sqlite3.

The local dev database is SQLite; production is Postgres on Railway. There is no
pg_dump / psql / pgloader on this machine, so this copies table by table with
psycopg2 (already installed) and writes a real Django-shaped SQLite file:

  1. build an EMPTY SQLite database by running `manage.py migrate` against it, so
     the schema is exactly what the current models describe;
  2. open production READ-ONLY and copy every table's rows into it, converting
     values the way Django's own SQLite backend would;
  3. write content/db_snapshot_meta.json so nobody has to do forensics to work out
     how old the file is (see docs/scheduling-redesign/00_DATA_AUDIT_AND_INVENTORY.md
     section A1 for why that mattered);
  4. back the old file up to content/db_backup_<timestamp>.sqlite3 (gitignored) and
     move the new one into place.

USAGE
    # Bash / git-bash
    PROD_DATABASE_URL='postgresql://...' python scripts/pull_prod_snapshot.py --dry-run
    PROD_DATABASE_URL='postgresql://...' python scripts/pull_prod_snapshot.py

    # PowerShell
    $env:PROD_DATABASE_URL = 'postgresql://...'
    python scripts/pull_prod_snapshot.py --dry-run
    python scripts/pull_prod_snapshot.py

Get the URL from the Railway dashboard: Postgres service -> Variables ->
DATABASE_PUBLIC_URL. Use the PUBLIC one. The plain DATABASE_URL points at
`postgres.railway.internal`, which only resolves from inside Railway's network and
will simply time out from a laptop.

SAFETY
  * Production is opened in a READ ONLY transaction with a statement timeout. This
    script cannot write to production.
  * The `manage.py migrate` step is forced onto SQLite and DATABASE_URL is stripped
    from its environment, so it can never migrate production even if you launch this
    through `railway run` (which injects DATABASE_URL).
  * Nothing replaces content/db.sqlite3 until every table has copied successfully.
  * The URL is never printed or written to disk.

The file contains real customer PII and payment records. It is gitignored
(`db.sqlite3*`, `content/db_backup_*.sqlite3`); keep it that way.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid as uuid_mod
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "content" / "db.sqlite3"
META = REPO / "content" / "db_snapshot_meta.json"

BATCH = 5000
STATEMENT_TIMEOUT_MS = 15 * 60 * 1000

# Built locally by `migrate`; prod's ledger would not match a schema this file
# just created from the working tree. Everything else is copied verbatim.
SKIP_TABLES = {"django_migrations"}

# Tables that exist in production but that no current model describes, so
# `migrate` cannot recreate them and nothing in the app can read them. Verified
# 2026-08-21; each needs a reason, and anything NOT on this list still stops the
# copy so a genuinely missing feature table is never waved through.
#   django_site        django.contrib.sites is not in INSTALLED_APPS
#   users_driver       the Driver model moved from `users` to `drivers`; the old
#   users_driver_legs  table and its M2M were left behind, no model, no readers
LEGACY_ORPHAN_TABLES = {"django_site", "users_driver", "users_driver_legs"}

# Tables whose max(created_at)-style column tells us how fresh the snapshot is.
FRESHNESS_PROBES = [
    ("reservations_reservation", "created_at"),
    ("reservations_quote", "created_at"),
    ("reservations_lead", "created_at"),
    ("reservations_legstatus", "timestamp"),
    ("payment_payment", "created_at"),
]


def log(msg: str) -> None:
    print(msg, flush=True)


def resolve_source_url() -> str:
    url = os.environ.get("PROD_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    # Paste damage. A trailing space inside the quotes makes Postgres look for a
    # database named "railway " and fail with a message that blames the database
    # rather than the whitespace; surrounding quotes survive some shells too.
    url = url.strip().strip("'\"").strip()
    if not url:
        sys.exit(
            "No source URL.\n"
            "  Set PROD_DATABASE_URL to the Railway Postgres DATABASE_PUBLIC_URL.\n"
            "  Railway dashboard -> Postgres service -> Variables -> DATABASE_PUBLIC_URL."
        )
    if ".railway.internal" in url:
        sys.exit(
            "That is the INTERNAL Railway URL (postgres.railway.internal). It only\n"
            "resolves inside Railway's network and will time out from here.\n"
            "Use DATABASE_PUBLIC_URL instead (host looks like <something>.proxy.rlwy.net)."
        )
    return url


def build_empty_schema(sqlite_path: Path) -> None:
    """Run `manage.py migrate` against a fresh SQLite file to create the schema."""
    tmpdir = sqlite_path.parent
    settings_mod = "snapshot_settings"
    (tmpdir / f"{settings_mod}.py").write_text(
        "from business.settings import *  # noqa: F401,F403\n"
        "DATABASES = {'default': {\n"
        "    'ENGINE': 'django.db.backends.sqlite3',\n"
        f"    'NAME': r'{sqlite_path}',\n"
        "}}\n"
        "DEBUG = False\n"
        "# Guard: this module must never point at anything but the new SQLite file.\n"
        "assert DATABASES['default']['ENGINE'].endswith('sqlite3')\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    # A `railway run` invocation injects DATABASE_URL; business/settings.py switches to
    # Postgres whenever it is set. Strip it so migrate cannot touch production.
    env.pop("DATABASE_URL", None)
    env["ENABLE_DEBUG_TOOLBAR"] = "0"
    env["PYTHONPATH"] = os.pathsep.join([str(tmpdir), str(REPO), env.get("PYTHONPATH", "")])

    log("  running manage.py migrate against the new empty file ...")
    proc = subprocess.run(
        [sys.executable, "manage.py", "migrate", "--noinput", f"--settings={settings_mod}"],
        cwd=REPO, env=env, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-4000:] + "\n" + proc.stderr[-4000:] + "\n")
        sys.exit("migrate failed while building the empty schema — see output above.")
    applied = sum(1 for ln in proc.stdout.splitlines() if " OK" in ln)
    log(f"  schema built ({applied} migrations applied)")


def adapt(value):
    """Convert one Postgres value the way Django's SQLite backend would store it."""
    if value is None or isinstance(value, (int, float, str, bytes)):
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Decimal):
        # Django registers Decimal -> str; the column's NUMERIC affinity turns it
        # back into INTEGER/REAL on storage, which is what the existing file holds.
        return str(value)
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return str(value)
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return round(value.total_seconds() * 1_000_000)
    if isinstance(value, uuid_mod.UUID):
        return value.hex
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def pg_tables(pg) -> dict[str, list[str]]:
    """{table: [column, ...]} for every base table in the public schema."""
    with pg.cursor() as cur:
        cur.execute(
            "SELECT c.table_name, c.column_name "
            "FROM information_schema.columns c "
            "JOIN information_schema.tables t "
            "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
            "WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
            "ORDER BY c.table_name, c.ordinal_position"
        )
        out: dict[str, list[str]] = {}
        for table, column in cur.fetchall():
            out.setdefault(table, []).append(column)
        return out


def sqlite_tables(con: sqlite3.Connection) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for (name,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        out[name] = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
    return out


def assert_orm_not_loaded() -> None:
    """Refuse to copy if Django's ORM is importable in THIS process.

    This is the guarantee that an import cannot send anything. Confirmation
    emails, guest SMS and the GoHighLevel follow-up sequences all hang off
    `post_save` / `pre_save` handlers registered in reservations/signals.py,
    payment/signals.py and ops/signals.py. Those handlers only ever run through
    `Model.save()`. The copy below writes with raw sqlite3 INSERT statements, so
    no model is ever instantiated and no signal can fire — but that is a property
    of the code, and code changes. This check makes it an enforced invariant: the
    moment someone imports a model here "just to look something up", the copy
    stops instead of quietly acquiring the ability to email customers.

    `manage.py migrate` does load Django, but it runs in a SEPARATE subprocess,
    against an EMPTY database, and every AppConfig.ready() returns early for
    management commands, so no scheduler thread starts either.
    """
    if "django" in sys.modules:
        sys.exit(
            "refusing to copy: django is imported in this process.\n"
            "The copy path must stay ORM-free so no post_save signal (confirmation\n"
            "email, guest SMS, GHL sequence) can fire on imported rows."
        )


def copy_table(pg, con: sqlite3.Connection, table: str, columns: list[str]) -> int:
    # `manage.py migrate` fires Django's post_migrate signal, which auto-populates
    # django_content_type and auth_permission (one row per model/permission) in the
    # fresh schema before a single production row is copied. Left alone, production's
    # rows for those same tables then collide on id. Production is authoritative for
    # every table here, so clear whatever migrate seeded before inserting — cheap
    # (everything else is already empty) and safe for any table future Django
    # versions decide to auto-populate the same way.
    con.execute(f'DELETE FROM "{table}"')

    quoted = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    insert = f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})'
    # A named cursor streams server-side instead of buffering the whole table.
    cur = pg.cursor(name=f"snap_{table}_{int(time.time())}")
    cur.itersize = BATCH
    cur.execute(f'SELECT {quoted} FROM "{table}"')
    total = 0
    while True:
        rows = cur.fetchmany(BATCH)
        if not rows:
            break
        con.executemany(insert, [tuple(adapt(v) for v in row) for row in rows])
        total += len(rows)
    cur.close()
    con.commit()
    return total


def migration_drift(pg) -> tuple[list[str], list[str]]:
    """(applied in prod but absent from this checkout, present here but unapplied).

    The schema this script builds comes from `manage.py migrate` on the working
    tree, so a checkout that is behind the deploy silently cannot create some of
    production's tables. Comparing django_migrations against the migration files on
    disk says exactly which commit is missing, before anything is copied.
    """
    with pg.cursor() as cur:
        cur.execute("SELECT app, name FROM django_migrations ORDER BY app, name")
        applied = cur.fetchall()

    on_disk: set[tuple[str, str]] = set()
    for app_dir in REPO.iterdir():
        migrations = app_dir / "migrations"
        if app_dir.is_dir() and migrations.is_dir():
            for path in migrations.glob("[0-9]*.py"):
                on_disk.add((app_dir.name, path.stem))

    known_apps = {app for app, _ in on_disk}
    behind = [f"{app}.{name}" for app, name in applied
              if app in known_apps and (app, name) not in on_disk]
    ahead = [f"{app}.{name}" for app, name in sorted(on_disk)
             if (app, name) not in set(applied) and app in {a for a, _ in applied}]
    return behind, ahead


def probe_freshness(pg, tables: dict[str, list[str]]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    with pg.cursor() as cur:
        for table, column in FRESHNESS_PROBES:
            if table not in tables or column not in tables[table]:
                continue
            cur.execute(f'SELECT max("{column}") FROM "{table}"')
            value = cur.fetchone()[0]
            out[f"{table}.{column}"] = value.isoformat() if value else None
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="connect, report table/row counts and the freshness probes, "
                         "then stop without building or replacing anything")
    ap.add_argument("--out", type=Path, default=None,
                    help="write the snapshot here instead of replacing content/db.sqlite3")
    ap.add_argument("--no-backup", action="store_false", dest="keep_backup",
                    help="do not back up the file being replaced (default: back it up)")
    ap.add_argument("--allow-schema-drift", action="store_true",
                    help="continue even when production has tables this working tree "
                         "cannot create. Those tables are SKIPPED — data is lost from "
                         "the copy. Only use this if you know the tables do not matter.")
    args = ap.parse_args()

    url = resolve_source_url()

    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 is not installed:  python -m pip install psycopg2-binary")

    log("connecting to production (read-only) ...")
    pg = psycopg2.connect(url, connect_timeout=20,
                          options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")
    pg.set_session(readonly=True, autocommit=False)
    with pg.cursor() as cur:
        cur.execute("SELECT current_database(), version()")
        dbname, version = cur.fetchone()
    log(f"  connected to {dbname} ({version.split(',')[0]})")

    prod = pg_tables(pg)
    log(f"  {len(prod)} base tables in production")

    freshness = probe_freshness(pg, prod)
    log("  freshness probes (max timestamp per stream):")
    for key, value in freshness.items():
        log(f"    {key:45s} {value}")

    behind, ahead = migration_drift(pg)
    if behind:
        log("")
        log(f"  note: {len(behind)} ledger entries in production have no migration file here.")
        log("        Usually squashed/renamed/deleted migrations — Django never removes the")
        log("        django_migrations row when the file goes. Harmless on its own; the")
        log("        table comparison below is what actually decides whether this checkout")
        log("        can hold production's schema.")
        for name in behind:
            log(f"           {name}")
    if ahead:
        log(f"  note: this tree has {len(ahead)} migration(s) not applied in production: "
            f"{', '.join(ahead[:4])}{' ...' if len(ahead) > 4 else ''}")

    # The authoritative check. Build the schema this checkout produces and diff the
    # TABLE lists — a stale ledger entry proves nothing either way, but a table that
    # exists in production and cannot be created here would be silently lost.
    tmpdir = Path(tempfile.mkdtemp(prefix="gtc_snapshot_"))
    new_db = tmpdir / "db.sqlite3"
    log(f"building the schema this checkout produces, to compare against production")
    build_empty_schema(new_db)

    con = sqlite3.connect(new_db)
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("PRAGMA journal_mode = OFF")
    con.execute("PRAGMA synchronous = OFF")
    local = sqlite_tables(con)
    log(f"  production {len(prod)} tables · this checkout {len(local)} tables")

    only_prod = sorted(set(prod) - set(local) - SKIP_TABLES)
    orphans = [t for t in only_prod if t in LEGACY_ORPHAN_TABLES]
    unexpected = [t for t in only_prod if t not in LEGACY_ORPHAN_TABLES]
    only_local = sorted(set(local) - set(prod) - SKIP_TABLES)

    if orphans:
        log(f"  skipping {len(orphans)} known-orphan table(s) in production that no current "
            f"model describes: {', '.join(orphans)}")
    if unexpected:
        log("")
        log(f"  STOP: production has {len(unexpected)} table(s) this working tree cannot create:")
        for t in unexpected:
            log(f"           {t}")
        log("         The schema here is built by `manage.py migrate` from your checkout, so")
        log("         those tables would be silently missing from the copy. Your branch is")
        log("         behind what is deployed.")
        log("         Fix: check out the commit that is actually deployed, then re-run.")
        log("         Override (loses those tables): --allow-schema-drift")
        # A dry run writes nothing, so aborting here would just hide the row counts.
        if not args.allow_schema_drift and not args.dry_run:
            con.close()
            pg.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            return 2
        if args.allow_schema_drift:
            log("         --allow-schema-drift set; continuing without them.")
    if only_local:
        log(f"  WARNING: in the local schema but not in production (left empty): {only_local}")
        log("           your working tree has unmigrated model changes; harmless here.")

    if args.dry_run:
        with pg.cursor() as cur:
            log("  row counts (top 15 tables):")
            counts = []
            for table in prod:
                cur.execute(f'SELECT count(*) FROM "{table}"')
                counts.append((cur.fetchone()[0], table))
            for n, table in sorted(counts, reverse=True)[:15]:
                log(f"    {n:>9,}  {table}")
            log(f"  TOTAL {sum(n for n, _ in counts):,} rows")
        con.close()
        pg.close()
        shutil.rmtree(tmpdir, ignore_errors=True)
        log("\ndry run — nothing written.")
        return 0

    assert_orm_not_loaded()
    log("copying tables ... (raw sqlite3 INSERT — no ORM, so no signal can fire)")
    started = time.time()
    counts: dict[str, int] = {}
    for table in sorted(set(prod) & set(local)):
        if table in SKIP_TABLES:
            continue
        columns = [c for c in local[table] if c in prod[table]]
        missing = [c for c in local[table] if c not in prod[table]]
        extra = [c for c in prod[table] if c not in local[table]]
        if missing or extra:
            log(f"  ! {table}: column drift — local-only {missing}, prod-only {extra}")
        n = copy_table(pg, con, table, columns)
        counts[table] = n
        if n:
            log(f"  {n:>9,}  {table}")

    con.commit()
    con.execute("PRAGMA foreign_keys = ON")
    con.close()
    pg.close()
    elapsed = time.time() - started
    log(f"copied {sum(counts.values()):,} rows across {len(counts)} tables in {elapsed:.0f}s")

    meta = {
        "pulled_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source_database": dbname,
        "freshness_probes": freshness,
        "tables": len(counts),
        "rows": sum(counts.values()),
        "row_counts": dict(sorted(counts.items())),
        "skipped_tables": sorted(SKIP_TABLES),
        "note": "Produced by scripts/pull_prod_snapshot.py. Schema built by manage.py "
                "migrate from the working tree; rows copied from production read-only.",
    }

    destination = args.out or TARGET
    if destination.exists() and args.keep_backup:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = destination.parent / f"db_backup_{stamp}.sqlite3"
        log(f"backing up the current file to {backup.name}")
        shutil.move(str(destination), str(backup))

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(new_db), str(destination))
    shutil.rmtree(tmpdir, ignore_errors=True)
    (destination.parent / "db_snapshot_meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8")

    log(f"\nwrote {destination}  ({destination.stat().st_size / 1e6:.0f} MB)")
    log(f"wrote {destination.parent / 'db_snapshot_meta.json'}")
    log("\nverify with:")
    log("  python docs/scheduling-redesign/analysis/00_snapshot_provenance.py | head -40")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
