"""Read-only operational intelligence analysis for Grayson Towncar.

The module intentionally uses only the Python standard library so the analysis
can run in a minimal environment. It opens SQLite with ``mode=ro`` and enables
``PRAGMA query_only`` before issuing any query.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_SNAPSHOT = Path("scratch/operations_intelligence/prod_snapshot.sqlite3")
PRELIMINARY_DATABASE = Path("content/db.sqlite3")
DEFAULT_CUTOFF = date(2026, 7, 31)
OPERATING_TZ = ZoneInfo("America/New_York")

CORE_TABLES = {
    "Reservations": "reservations_reservation",
    "Legs": "reservations_leg",
    "Status events": "reservations_legstatus",
    "Flights": "reservations_flight",
    "Audit events": "reservations_auditlog",
    "Schedule snapshots": "reservations_schedulesnapshot",
    "Schedule snapshot entries": "reservations_schedulesnapshotentry",
    "Schedule drafts": "reservations_scheduledraft",
    "Draft assignments": "reservations_draftassignment",
    "Draft events": "reservations_scheduledraftevent",
    "Route timing buckets": "reservations_routetimingmetric",
    "Daily driver capacity": "reservations_driverdailycapacity",
    "Demand patterns": "reservations_demandpattern",
    "Driver locations": "reservations_driverlocation",
    "Drivers": "drivers_driver",
    "Fleet vehicles": "drivers_fleetvehicle",
    "Driver-vehicle assignments": "drivers_drivervehicleassignment",
    "Leg payments": "drivers_legpayment",
    "Customer payments": "payment_payment",
    "Operational tasks": "ops_operationaltask",
    "Communication attempts": "ops_communicationattempt",
    "Staff activity": "ops_staffactivity",
    "Email log": "ops_emaillog",
    "Time-clock shifts": "ops_timeclockshift",
    "Follow-up tasks": "ghl_integration_followuptask",
    "Lead activities": "ghl_integration_leadactivity",
    "GHL sync logs": "ghl_integration_ghlsynclog",
}

FRESHNESS_FIELDS = {
    "reservations_reservation": "updated_at",
    "reservations_leg": "status_changed_at",
    "reservations_legstatus": "timestamp",
    "reservations_flight": "last_updated",
    "reservations_auditlog": "timestamp",
    "reservations_schedulesnapshot": "created_at",
    "reservations_scheduledraft": "created_at",
    "reservations_routetimingmetric": "last_calculated",
    "payment_payment": "updated_at",
    "drivers_legpayment": "updated_at",
    "ops_operationaltask": "updated_at",
    "ops_communicationattempt": "created_at",
    "ops_staffactivity": "created_at",
    "ops_emaillog": "sent_at",
    "ops_timeclockshift": "updated_at",
    "ghl_integration_followuptask": "created_at",
    "ghl_integration_leadactivity": "created_at",
    "ghl_integration_ghlsynclog": "created_at",
}

STATIC_DRIVE_MINUTES = {
    ("MCO Terminal", "Disney Resort"): 30,
    ("Disney Resort", "MCO Terminal"): 30,
    ("MCO Terminal", "Universal Resort"): 25,
    ("Universal Resort", "MCO Terminal"): 25,
    ("MCO Terminal", "Port Canaveral Area"): 55,
    ("Port Canaveral Area", "MCO Terminal"): 55,
    ("MCO Terminal", "Other Hotel"): 25,
    ("Other Hotel", "MCO Terminal"): 25,
    ("MCO Terminal", "Residential"): 30,
    ("Residential", "MCO Terminal"): 30,
    ("MCO Terminal", "Airport Hotel"): 12,
    ("Airport Hotel", "MCO Terminal"): 12,
    ("Disney Resort", "Port Canaveral Area"): 72,
    ("Port Canaveral Area", "Disney Resort"): 72,
    ("Disney Resort", "Universal Resort"): 28,
    ("Universal Resort", "Disney Resort"): 28,
    ("Disney Resort", "Other Hotel"): 25,
    ("Other Hotel", "Disney Resort"): 25,
    ("Disney Resort", "Disney Resort"): 12,
    ("MCO Terminal", "MCO Terminal"): 2,
    ("SFB Terminal", "SFB Terminal"): 2,
    ("Airport Hotel", "Airport Hotel"): 10,
    ("Other Hotel", "Other Hotel"): 15,
    ("Residential", "Residential"): 15,
    ("Port Canaveral Area", "Port Canaveral Area"): 10,
    ("Other", "Other"): 20,
    ("Universal Resort", "Port Canaveral Area"): 60,
    ("Port Canaveral Area", "Universal Resort"): 60,
    ("Universal Resort", "Other Hotel"): 15,
    ("Other Hotel", "Universal Resort"): 15,
    ("Universal Resort", "Universal Resort"): 10,
    ("SFB Terminal", "Disney Resort"): 60,
    ("Disney Resort", "SFB Terminal"): 60,
    ("SFB Terminal", "Universal Resort"): 49,
    ("SFB Terminal", "Port Canaveral Area"): 70,
    ("Port Canaveral Area", "SFB Terminal"): 70,
    ("Airport Hotel", "Disney Resort"): 25,
    ("Disney Resort", "Airport Hotel"): 25,
    ("Airport Hotel", "Universal Resort"): 20,
    ("Universal Resort", "Airport Hotel"): 20,
    ("SFB Terminal", "MCO Terminal"): 60,
    ("MCO Terminal", "SFB Terminal"): 60,
    ("SFB Terminal", "Other Hotel"): 55,
    ("Other Hotel", "SFB Terminal"): 55,
    ("SFB Terminal", "Airport Hotel"): 45,
    ("Airport Hotel", "SFB Terminal"): 45,
    ("SFB Terminal", "Residential"): 55,
    ("Residential", "SFB Terminal"): 55,
    ("Airport Hotel", "Port Canaveral Area"): 55,
    ("Port Canaveral Area", "Airport Hotel"): 55,
    ("Other Hotel", "Port Canaveral Area"): 55,
    ("Port Canaveral Area", "Other Hotel"): 55,
}
DEFAULT_DRIVE_MINUTES = 35

STATUS_ANALYSIS_SQL = """
WITH event_bounds AS (
  SELECT
    l.id AS leg_id,
    l.pickup_date,
    l.pickup_time,
    l.status AS current_status,
    l.driver_id,
    r.trip_type,
    MIN(CASE WHEN s.status = 'on-the-way' THEN s.timestamp END) AS first_on_way,
    MIN(CASE WHEN s.status = 'on-location' THEN s.timestamp END) AS first_on_location,
    MIN(CASE WHEN s.status = 'picked-up' THEN s.timestamp END) AS first_picked_up,
    MIN(CASE WHEN s.status = 'completed' THEN s.timestamp END) AS first_completed,
    MAX(CASE WHEN s.status = 'on-the-way' THEN s.timestamp END) AS last_on_way,
    MAX(CASE WHEN s.status = 'on-location' THEN s.timestamp END) AS last_on_location,
    MAX(CASE WHEN s.status = 'picked-up' THEN s.timestamp END) AS last_picked_up,
    MAX(CASE WHEN s.status = 'completed' THEN s.timestamp END) AS last_completed,
    SUM(CASE WHEN s.status = 'on-the-way' THEN 1 ELSE 0 END) AS count_on_way,
    SUM(CASE WHEN s.status = 'on-location' THEN 1 ELSE 0 END) AS count_on_location,
    SUM(CASE WHEN s.status = 'picked-up' THEN 1 ELSE 0 END) AS count_picked_up,
    SUM(CASE WHEN s.status = 'completed' THEN 1 ELSE 0 END) AS count_completed
  FROM reservations_leg l
  JOIN reservations_reservation r ON r.id = l.reservation_id
  LEFT JOIN reservations_legstatus s ON s.leg_id = l.id
  WHERE l.pickup_date IS NOT NULL
  GROUP BY l.id, l.pickup_date, l.pickup_time, l.status, l.driver_id, r.trip_type
)
SELECT * FROM event_bounds
ORDER BY pickup_date, pickup_time, leg_id
""".strip()

ROUTE_METRIC_SQL = """
SELECT trip_type, pickup_location_category, dropoff_location_category,
       time_of_day_category, day_type, sample_count, avg_airport_dwell_time,
       median_airport_dwell_time, p75_airport_dwell_time, p90_airport_dwell_time,
       avg_drive_time, median_drive_time, p75_drive_time, p90_drive_time,
       avg_total_time, median_total_time, p75_total_time, p90_total_time,
       last_calculated
FROM reservations_routetimingmetric
ORDER BY sample_count DESC
""".strip()

ROADMAP_PRIORITY_SQL = """
WITH roadmap(phase, recommendation, impact, effort, confidence) AS (
  VALUES
    (1, 'Canonical transition service', 5, 3, 'High'),
    (1, 'Immutable operational event contract', 5, 3, 'High'),
    (1, 'Flight forecast observation history', 5, 3, 'High'),
    (1, 'Schedule decision snapshots and reason codes', 5, 3, 'High'),
    (1, 'Nightly data-quality gates', 4, 2, 'High'),
    (2, 'Governed metric refresh pipeline', 4, 3, 'High'),
    (2, 'Weekly operations scorecard', 4, 3, 'Medium'),
    (2, 'Payment reconciliation checks', 3, 2, 'Medium'),
    (3, 'Duration and demand shadow baselines', 4, 3, 'Medium'),
    (3, 'Dispatcher exception decision support', 4, 4, 'Medium'),
    (4, 'Production predictive automation', 3, 5, 'Low')
)
SELECT phase, recommendation, impact, effort, confidence,
       (impact * 2.0) - effort AS priority_score
FROM roadmap
ORDER BY phase, priority_score DESC, recommendation
""".strip()


@dataclass
class SourceContext:
    requested_path: str
    selected_path: str
    relative_path: str
    source_tier: str
    fresh_snapshot_present: bool


class ReadOnlySQLite:
    def __init__(self, path: Path):
        self.path = path.resolve()
        uri = f"file:{self.path.as_posix()}?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only = ON")

    def close(self) -> None:
        self.connection.close()

    def rows(self, sql: str, parameters: Iterable[Any] = ()) -> list[dict[str, Any]]:
        cursor = self.connection.execute(sql, tuple(parameters))
        return [dict(row) for row in cursor.fetchall()]

    def scalar(self, sql: str, parameters: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.connection.execute(sql, tuple(parameters)).fetchone()
        return row[0] if row and row[0] is not None else default

    def table_exists(self, table: str) -> bool:
        return bool(
            self.scalar(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
                [table],
                0,
            )
        )


def relative_to_repo(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return path.name


def choose_database(requested: str | Path, allow_preliminary: bool) -> SourceContext:
    requested_path = Path(requested)
    if not requested_path.is_absolute():
        requested_path = REPOSITORY_ROOT / requested_path
    if requested_path.exists() and requested_path.stat().st_size > 0:
        selected = requested_path
        source_tier = "fresh production snapshot"
        fresh = relative_to_repo(requested_path) == EXPECTED_SNAPSHOT.as_posix()
    elif allow_preliminary:
        selected = REPOSITORY_ROOT / PRELIMINARY_DATABASE
        if not selected.exists() or selected.stat().st_size == 0:
            raise FileNotFoundError(
                f"Neither requested snapshot {requested_path} nor preliminary database {selected} exists"
            )
        source_tier = "preliminary working copy"
        fresh = False
    else:
        raise FileNotFoundError(f"Required read-only snapshot is missing: {requested_path}")
    return SourceContext(
        requested_path=relative_to_repo(requested_path),
        selected_path=str(selected.resolve()),
        relative_path=relative_to_repo(selected),
        source_tier=source_tier,
        fresh_snapshot_present=fresh,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_datetime(value: Any, assume_utc: bool = True) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC if assume_utc else OPERATING_TZ)
    return parsed.astimezone(OPERATING_TZ)


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def rate(numerator: int | float, denominator: int | float) -> float | None:
    return float(numerator) / float(denominator) if denominator else None


def format_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def format_number(value: int | float | None, digits: int = 0) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{digits}f}"


def markdown_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    """Render a compact, portable Markdown table for narrative report blocks."""
    header = "| " + " | ".join(label for _, label in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = []
    for row in rows:
        values = []
        for field, _ in columns:
            value = row.get(field)
            if value is None:
                rendered = "—"
            elif isinstance(value, float):
                rendered = format_number(value, 2)
            else:
                rendered = str(value)
            values.append(rendered.replace("|", "/").replace("\n", " "))
        body.append("| " + " | ".join(values) + " |")
    return "\n".join([header, divider, *body])


def markdown_records(
    rows: list[dict[str, Any]],
    title_field: str,
    fields: list[tuple[str, str]],
) -> str:
    """Render mobile-friendly evidence records without a page-wide table."""
    records = []
    for row in rows:
        title = str(row.get(title_field) or "Untitled").replace("\n", " ")
        lines = [f"### {title}"]
        for field, label in fields:
            value = row.get(field)
            rendered = "—" if value is None else str(value).replace("\n", " ")
            lines.append(f"- **{label}:** {rendered}")
        records.append("\n".join(lines))
    return "\n\n".join(records)


def month_label(month: str) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%b %Y")


def categorize_location(value: Any) -> str:
    text = str(value or "").lower()
    if "orlando international" in text or " mco" in f" {text}" or text.strip() == "mco":
        return "MCO Terminal"
    if "sanford" in text and ("airport" in text or "terminal" in text or "sfb" in text):
        return "SFB Terminal"
    if "port canaveral" in text or "cruise terminal" in text:
        return "Port Canaveral Area"
    if "disney" in text:
        return "Disney Resort"
    if "universal" in text:
        return "Universal Resort"
    if "airport" in text and ("hotel" in text or "inn" in text):
        return "Airport Hotel"
    if any(word in text for word in ("hotel", "resort", "inn", "suites")):
        return "Other Hotel"
    if any(word in text for word in ("home", "residence", "villa", "airbnb", "vrbo")):
        return "Residential"
    return "Other"


def classify_trip_type(pickup_location: Any, dropoff_location: Any) -> str:
    """Mirror the Leg.get_trip_type operating categories without importing Django."""
    pickup_category = categorize_location(pickup_location)
    dropoff_category = categorize_location(dropoff_location)
    if "Port Canaveral Area" in (pickup_category, dropoff_category):
        return "cruise"
    pickup_airport = pickup_category in {"MCO Terminal", "SFB Terminal"}
    dropoff_airport = dropoff_category in {"MCO Terminal", "SFB Terminal"}
    if pickup_airport and not dropoff_airport:
        return "arrival"
    if dropoff_airport and not pickup_airport:
        return "return"
    return "other"


def status_chain_valid(on_way: datetime | None, picked: datetime | None, completed: datetime | None) -> bool:
    if not all((on_way, picked, completed)):
        return False
    otw_to_pickup = (picked - on_way).total_seconds() / 60
    pickup_to_complete = (completed - picked).total_seconds() / 60
    return bool(
        on_way < picked < completed
        and 1 <= otw_to_pickup <= 180
        and 2 <= pickup_to_complete <= 180
    )


def source_profile(database: ReadOnlySQLite, context: SourceContext, cutoff: date) -> dict[str, Any]:
    path = Path(context.selected_path)
    quick_check = database.scalar("PRAGMA quick_check", default="not run")
    table_count = database.scalar(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'", default=0
    )
    migration_count = database.scalar("SELECT COUNT(*) FROM django_migrations", default=0)
    latest_migration = database.scalar("SELECT MAX(applied) FROM django_migrations")
    return {
        "requested_path": context.requested_path,
        "selected_path": context.relative_path,
        "source_tier": context.source_tier,
        "fresh_snapshot_present": context.fresh_snapshot_present,
        "size_bytes": path.stat().st_size,
        "modified_utc": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
        "sha256": sha256_file(path),
        "quick_check": quick_check,
        "table_count": table_count,
        "migration_count": migration_count,
        "latest_migration": latest_migration,
        "analysis_cutoff": cutoff.isoformat(),
        "timezone": str(OPERATING_TZ),
    }


def build_table_inventory(database: ReadOnlySQLite) -> list[dict[str, Any]]:
    output = []
    for label, table in CORE_TABLES.items():
        if not database.table_exists(table):
            output.append(
                {
                    "domain": label,
                    "table": table,
                    "rows": None,
                    "first_record": None,
                    "latest_record": None,
                    "state": "missing table",
                }
            )
            continue
        row_count = database.scalar(f'SELECT COUNT(*) FROM "{table}"', default=0)
        freshness = FRESHNESS_FIELDS.get(table)
        first_value = latest_value = None
        if freshness:
            first_value = database.scalar(f'SELECT MIN("{freshness}") FROM "{table}"')
            latest_value = database.scalar(f'SELECT MAX("{freshness}") FROM "{table}"')
        output.append(
            {
                "domain": label,
                "table": table,
                "rows": row_count,
                "first_record": str(first_value)[:19] if first_value else None,
                "latest_record": str(latest_value)[:19] if latest_value else None,
                "state": "empty" if row_count == 0 else "available",
            }
        )
    return output


def analyze_status_history(database: ReadOnlySQLite, cutoff: date) -> dict[str, Any]:
    raw = database.rows(STATUS_ANALYSIS_SQL)
    valid_year_min = 2015
    valid_year_max = cutoff.year + 2
    completed_rows = []
    monthly = defaultdict(lambda: Counter())
    semantic_disagreement = 0
    repeated_leg_status_pairs = 0
    late_backfill_count = 0

    for row in raw:
        pickup_date = parse_date(row["pickup_date"])
        if not pickup_date or not (valid_year_min <= pickup_date.year <= valid_year_max):
            continue
        if pickup_date > cutoff:
            continue
        if row["current_status"] != "completed":
            continue
        month = pickup_date.strftime("%Y-%m")
        first_on_way = parse_datetime(row["first_on_way"])
        first_picked = parse_datetime(row["first_picked_up"])
        first_completed = parse_datetime(row["first_completed"])
        last_on_way = parse_datetime(row["last_on_way"])
        last_picked = parse_datetime(row["last_picked_up"])
        last_completed = parse_datetime(row["last_completed"])
        earliest_valid = status_chain_valid(first_on_way, first_picked, first_completed)
        latest_valid = status_chain_valid(last_on_way, last_picked, last_completed)
        if earliest_valid != latest_valid:
            semantic_disagreement += 1
        repeated = any(
            int(row[key] or 0) > 1
            for key in ("count_on_way", "count_on_location", "count_picked_up", "count_completed")
        )
        repeated_leg_status_pairs += int(repeated)
        completion_lag_days = None
        timely_completion = False
        if first_completed:
            pickup_local = datetime.combine(pickup_date, datetime.min.time(), OPERATING_TZ)
            completion_lag_days = (first_completed - pickup_local).total_seconds() / 86400
            timely_completion = -1 <= completion_lag_days <= 3
            late_backfill_count += int(completion_lag_days > 30)
        monthly[month]["completed_legs"] += 1
        monthly[month]["timely_completed_event"] += int(timely_completion)
        monthly[month]["earliest_valid_chain"] += int(earliest_valid)
        monthly[month]["latest_valid_chain"] += int(latest_valid)
        monthly[month]["full_four_statuses"] += int(
            all(
                (
                    first_on_way,
                    parse_datetime(row["first_on_location"]),
                    first_picked,
                    first_completed,
                )
            )
        )
        completed_rows.append(
            {
                **row,
                "pickup_date_parsed": pickup_date,
                "first_on_way_parsed": first_on_way,
                "first_on_location_parsed": parse_datetime(row["first_on_location"]),
                "first_picked_parsed": first_picked,
                "first_completed_parsed": first_completed,
                "earliest_valid": earliest_valid,
                "latest_valid": latest_valid,
                "timely_completion": timely_completion,
            }
        )

    monthly_rows = []
    for month in sorted(monthly):
        values = monthly[month]
        total = values["completed_legs"]
        monthly_rows.append(
            {
                "month": f"{month}-01",
                "month_label": month_label(month),
                "completed_legs": total,
                "timely_completion_event_rate": rate(values["timely_completed_event"], total),
                "earliest_valid_chain_rate": rate(values["earliest_valid_chain"], total),
                "latest_valid_chain_rate": rate(values["latest_valid_chain"], total),
                "four_status_coverage_rate": rate(values["full_four_statuses"], total),
            }
        )

    instrumentation_start = None
    for item in monthly_rows:
        if (
            item["completed_legs"] >= 100
            and (item["timely_completion_event_rate"] or 0) >= 0.75
        ):
            instrumentation_start = parse_date(item["month"])
            break
    if instrumentation_start is None:
        instrumentation_start = cutoff - timedelta(days=180)

    cohort = [row for row in completed_rows if row["pickup_date_parsed"] >= instrumentation_start]
    chain_rate = rate(sum(row["earliest_valid"] for row in cohort), len(cohort))
    timely_rate = rate(sum(row["timely_completion"] for row in cohort), len(cohort))

    repeat_rows = database.rows(
        """
        SELECT status,
               COUNT(*) AS repeated_leg_status_pairs,
               SUM(occurrences - 1) AS extra_events,
               MAX(occurrences) AS max_occurrences
        FROM (
          SELECT leg_id, status, COUNT(*) AS occurrences
          FROM reservations_legstatus
          GROUP BY leg_id, status
          HAVING COUNT(*) > 1
        ) repeats
        GROUP BY status
        ORDER BY extra_events DESC
        """
    )
    for item in repeat_rows:
        item["status_label"] = str(item["status"]).replace("-", " ").title()

    exact_duplicate_events = database.scalar(
        """
        SELECT COALESCE(SUM(occurrences - 1), 0)
        FROM (
          SELECT leg_id, status, timestamp, COUNT(*) AS occurrences
          FROM reservations_legstatus
          GROUP BY leg_id, status, timestamp
          HAVING COUNT(*) > 1
        )
        """,
        default=0,
    )
    return {
        "all_rows": raw,
        "completed_rows": completed_rows,
        "monthly": monthly_rows,
        "instrumentation_start": instrumentation_start,
        "cohort_completed_legs": len(cohort),
        "cohort_chain_rate": chain_rate,
        "cohort_timely_completion_rate": timely_rate,
        "semantic_disagreement": semantic_disagreement,
        "legs_with_repeated_status": repeated_leg_status_pairs,
        "late_backfill_completed_events": late_backfill_count,
        "repeat_summary": repeat_rows,
        "exact_duplicate_events": exact_duplicate_events,
    }


def analyze_dates_and_categories(database: ReadOnlySQLite, cutoff: date) -> dict[str, Any]:
    invalid_pickup_dates = database.scalar(
        """
        SELECT COUNT(*) FROM reservations_leg
        WHERE pickup_date IS NOT NULL
          AND (CAST(substr(pickup_date, 1, 4) AS INTEGER) < 2015
               OR CAST(substr(pickup_date, 1, 4) AS INTEGER) > ?)
        """,
        [cutoff.year + 2],
        0,
    )
    max_pickup_date = database.scalar("SELECT MAX(pickup_date) FROM reservations_leg")
    reservation_statuses = database.rows(
        """
        SELECT status, COUNT(*) AS reservations
        FROM reservations_reservation
        GROUP BY status
        ORDER BY reservations DESC
        """
    )
    normalized_collisions = database.rows(
        """
        SELECT lower(replace(trim(status), 'cancelled', 'canceled')) AS normalized_status,
               COUNT(DISTINCT status) AS variants,
               GROUP_CONCAT(DISTINCT status) AS observed_values,
               COUNT(*) AS rows
        FROM reservations_reservation
        GROUP BY lower(replace(trim(status), 'cancelled', 'canceled'))
        HAVING COUNT(DISTINCT status) > 1
        """
    )
    return {
        "invalid_pickup_dates": invalid_pickup_dates,
        "max_pickup_date": max_pickup_date,
        "reservation_statuses": reservation_statuses,
        "normalized_collisions": normalized_collisions,
    }


def analyze_route_metrics(database: ReadOnlySQLite, cutoff: date) -> dict[str, Any]:
    rows = database.rows(ROUTE_METRIC_SQL)
    confidence = Counter()
    samples = Counter()
    stale = 0
    for row in rows:
        count = int(row["sample_count"] or 0)
        if count >= 20:
            bucket = "20+ samples"
            order = 1
        elif count >= 10:
            bucket = "10-19 samples"
            order = 2
        elif count >= 5:
            bucket = "5-9 samples"
            order = 3
        elif count >= 1:
            bucket = "1-4 samples"
            order = 4
        else:
            bucket = "0 samples"
            order = 5
        confidence[(order, bucket)] += 1
        samples[(order, bucket)] += count
        last = parse_datetime(row["last_calculated"])
        if not last or (datetime.combine(cutoff, datetime.min.time(), OPERATING_TZ) - last).days > 14:
            stale += 1
    confidence_rows = [
        {
            "order": order,
            "confidence_bucket": bucket,
            "route_buckets": confidence[(order, bucket)],
            "underlying_samples": samples[(order, bucket)],
        }
        for order, bucket in sorted(confidence)
    ]
    null_counts = {}
    for field in (
        "median_airport_dwell_time",
        "p75_airport_dwell_time",
        "median_drive_time",
        "p75_drive_time",
        "p90_drive_time",
        "median_total_time",
        "p75_total_time",
    ):
        null_counts[field] = sum(row[field] is None for row in rows)
    return {
        "rows": rows,
        "confidence": confidence_rows,
        "total_buckets": len(rows),
        "reliable_buckets": sum(item["route_buckets"] for item in confidence_rows if item["order"] <= 3),
        "high_confidence_buckets": sum(item["route_buckets"] for item in confidence_rows if item["order"] == 1),
        "stale_buckets_14d": stale,
        "null_counts": null_counts,
    }


def analyze_route_duration_baseline(
    database: ReadOnlySQLite, status: dict[str, Any], cutoff: date
) -> dict[str, Any]:
    leg_details = {
        row["id"]: row
        for row in database.rows(
            """
            SELECT l.id, l.pickup_location, l.dropoff_location, l.exclude_from_analytics,
                   d.driver_type, d.exclude_from_timing
            FROM reservations_leg l
            LEFT JOIN drivers_driver d ON d.id = l.driver_id
            """
        )
    }
    observations = []
    dwell_observations = []
    for row in status["completed_rows"]:
        detail = leg_details.get(row["leg_id"], {})
        if detail.get("driver_type") != "inhouse":
            continue
        if detail.get("exclude_from_analytics") or detail.get("exclude_from_timing"):
            continue
        picked = row["first_picked_parsed"]
        completed = row["first_completed_parsed"]
        if not picked or not completed:
            continue
        duration = (completed - picked).total_seconds() / 60
        if not 2 <= duration <= 180:
            continue
        pickup_category = categorize_location(detail.get("pickup_location"))
        dropoff_category = categorize_location(detail.get("dropoff_location"))
        operational_trip_type = classify_trip_type(
            detail.get("pickup_location"), detail.get("dropoff_location")
        )
        route_key = (operational_trip_type, pickup_category, dropoff_category)
        observations.append(
            {
                "pickup_date": row["pickup_date_parsed"],
                "route_key": route_key,
                "trip_type": operational_trip_type,
                "pickup_category": pickup_category,
                "dropoff_category": dropoff_category,
                "duration_minutes": duration,
                "static_minutes": STATIC_DRIVE_MINUTES.get(
                    (pickup_category, dropoff_category), DEFAULT_DRIVE_MINUTES
                ),
            }
        )
        on_location = row["first_on_location_parsed"]
        if operational_trip_type == "arrival" and on_location and picked:
            dwell = (picked - on_location).total_seconds() / 60
            if 0 <= dwell <= 180:
                dwell_observations.append(dwell)

    observations.sort(key=lambda item: item["pickup_date"])
    split_index = max(1, int(len(observations) * 0.8)) if observations else 0
    train = observations[:split_index]
    test = observations[split_index:]
    train_by_route: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for item in train:
        train_by_route[item["route_key"]].append(item["duration_minutes"])
    overall_train_median = statistics.median(
        [item["duration_minutes"] for item in train]
    ) if train else None
    static_errors = []
    baseline_errors = []
    for item in test:
        history = train_by_route.get(item["route_key"], [])
        prediction = statistics.median(history) if len(history) >= 10 else overall_train_median
        if prediction is None:
            continue
        static_errors.append(abs(item["duration_minutes"] - item["static_minutes"]))
        baseline_errors.append(abs(item["duration_minutes"] - prediction))

    route_groups: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for item in observations:
        route_groups[item["route_key"]].append(item["duration_minutes"])
    route_summary = []
    for key, values in route_groups.items():
        if len(values) < 10:
            continue
        trip_type, pickup_category, dropoff_category = key
        static = STATIC_DRIVE_MINUTES.get((pickup_category, dropoff_category), DEFAULT_DRIVE_MINUTES)
        median_value = statistics.median(values)
        route_summary.append(
            {
                "route": f"{pickup_category} -> {dropoff_category}",
                "trip_type": trip_type,
                "samples": len(values),
                "observed_median_minutes": round(median_value, 1),
                "observed_p75_minutes": round(percentile(values, 0.75) or 0, 1),
                "observed_p90_minutes": round(percentile(values, 0.90) or 0, 1),
                "static_minutes": static,
                "median_minus_static": round(median_value - static, 1),
            }
        )
    route_summary.sort(key=lambda item: (-item["samples"], item["route"], item["trip_type"]))
    static_mae = statistics.mean(static_errors) if static_errors else None
    baseline_mae = statistics.mean(baseline_errors) if baseline_errors else None
    improvement = (
        1 - baseline_mae / static_mae
        if static_mae and baseline_mae is not None
        else None
    )
    return {
        "eligible_observations": len(observations),
        "train_observations": len(train),
        "test_observations": len(test),
        "static_mae_minutes": round(static_mae, 1) if static_mae is not None else None,
        "historical_median_mae_minutes": round(baseline_mae, 1) if baseline_mae is not None else None,
        "mae_improvement": improvement,
        "dwell_samples": len(dwell_observations),
        "dwell_median_minutes": round(statistics.median(dwell_observations), 1) if dwell_observations else None,
        "dwell_p75_minutes": round(percentile(dwell_observations, 0.75) or 0, 1) if dwell_observations else None,
        "dwell_p90_minutes": round(percentile(dwell_observations, 0.90) or 0, 1) if dwell_observations else None,
        "route_summary": route_summary[:30],
    }


def analyze_demand_baseline(
    database: ReadOnlySQLite, start_date: date, cutoff: date
) -> dict[str, Any]:
    daily_rows = database.rows(
        """
        SELECT pickup_date, COUNT(*) AS scheduled_legs
        FROM reservations_leg
        WHERE pickup_date BETWEEN ? AND ?
          AND lower(trim(COALESCE(status, ''))) NOT IN ('cancelled', 'canceled')
        GROUP BY pickup_date
        ORDER BY pickup_date
        """,
        [start_date.isoformat(), cutoff.isoformat()],
    )
    daily = [
        (parse_date(row["pickup_date"]), int(row["scheduled_legs"]))
        for row in daily_rows
        if parse_date(row["pickup_date"])
    ]
    test_days = min(28, max(7, len(daily) // 5)) if len(daily) >= 35 else 0
    train = daily[:-test_days] if test_days else []
    test = daily[-test_days:] if test_days else []
    by_weekday: dict[int, list[int]] = defaultdict(list)
    for day_value, count in train:
        by_weekday[day_value.weekday()].append(count)
    overall = statistics.median([count for _, count in train]) if train else None
    weekday_errors = []
    naive_errors = []
    for day_value, actual in test:
        history = by_weekday.get(day_value.weekday(), [])
        prediction = statistics.median(history) if history else overall
        if prediction is None:
            continue
        weekday_errors.append(abs(actual - prediction))
        naive_errors.append(abs(actual - overall))

    monthly = database.rows(
        """
        SELECT substr(pickup_date, 1, 7) || '-01' AS month,
               COUNT(*) AS scheduled_legs
        FROM reservations_leg
        WHERE pickup_date BETWEEN date(?, '-17 months', 'start of month') AND ?
          AND lower(trim(COALESCE(status, ''))) NOT IN ('cancelled', 'canceled')
        GROUP BY substr(pickup_date, 1, 7)
        ORDER BY month
        """,
        [cutoff.isoformat(), cutoff.isoformat()],
    )
    return {
        "daily_rows": len(daily),
        "train_days": len(train),
        "test_days": len(test),
        "weekday_median_mae_legs": round(statistics.mean(weekday_errors), 2) if weekday_errors else None,
        "overall_median_mae_legs": round(statistics.mean(naive_errors), 2) if naive_errors else None,
        "monthly_volume": monthly,
    }


def analyze_operations(database: ReadOnlySQLite, start_date: date, cutoff: date) -> dict[str, Any]:
    parameters = [start_date.isoformat(), cutoff.isoformat()]
    leg_summary = database.rows(
        """
        SELECT
          COUNT(*) AS legs,
          SUM(CASE WHEN l.driver_id IS NOT NULL THEN 1 ELSE 0 END) AS assigned_legs,
          SUM(CASE WHEN d.driver_type = 'inhouse' THEN 1 ELSE 0 END) AS inhouse_legs,
          SUM(CASE WHEN d.driver_type = 'affiliate' THEN 1 ELSE 0 END) AS affiliate_legs,
          SUM(CASE WHEN l.status = 'completed' THEN 1 ELSE 0 END) AS completed_legs,
          COUNT(DISTINCT l.driver_id) AS assigned_drivers
        FROM reservations_leg l
        LEFT JOIN drivers_driver d ON d.id = l.driver_id
        WHERE l.pickup_date BETWEEN ? AND ?
          AND lower(trim(COALESCE(l.status, ''))) NOT IN ('cancelled', 'canceled')
        """,
        parameters,
    )[0]
    assigned = int(leg_summary["assigned_legs"] or 0)
    leg_summary["assignment_rate"] = rate(assigned, int(leg_summary["legs"] or 0))
    leg_summary["affiliate_share_of_assigned"] = rate(
        int(leg_summary["affiliate_legs"] or 0), assigned
    )

    assignment_leads = [
        float(row["lead_hours"])
        for row in database.rows(
            """
            SELECT (julianday(pickup_date || ' ' || pickup_time) - julianday(driver_assigned_at)) * 24 AS lead_hours
            FROM reservations_leg
            WHERE pickup_date BETWEEN ? AND ?
              AND driver_id IS NOT NULL
              AND driver_assigned_at IS NOT NULL
              AND pickup_time IS NOT NULL
            """,
            parameters,
        )
        if row["lead_hours"] is not None and -24 <= float(row["lead_hours"]) <= 24 * 365
    ]

    payment_summary = database.rows(
        """
        SELECT
          COUNT(*) AS completed_reservations,
          SUM(CASE WHEN is_paid = 0 THEN 1 ELSE 0 END) AS completed_not_marked_paid,
          SUM(CASE WHEN requires_manual_review = 1 THEN 1 ELSE 0 END) AS manual_review,
          SUM(CASE WHEN paid_amount + total_refunded + 0.01 < total_price THEN 1 ELSE 0 END) AS apparent_balance
        FROM reservations_reservation
        WHERE status = 'completed'
          AND created_at <= datetime(?, '+1 day')
        """,
        [cutoff.isoformat()],
    )[0]

    audit_min = database.scalar("SELECT MIN(timestamp) FROM reservations_auditlog")
    audit_max = database.scalar("SELECT MAX(timestamp) FROM reservations_auditlog")
    staff_min = database.scalar("SELECT MIN(created_at) FROM ops_staffactivity")
    communications_min = database.scalar("SELECT MIN(created_at) FROM ops_communicationattempt")

    snapshot_rows = database.rows(
        """
        SELECT s.schedule_date, s.id AS snapshot_id, s.created_at,
               e.leg_id, e.driver_id
        FROM reservations_schedulesnapshot s
        JOIN reservations_schedulesnapshotentry e ON e.snapshot_id = s.id
        ORDER BY s.schedule_date, s.created_at, s.id, e.leg_id
        """
    )
    states: dict[str, dict[int, dict[int, Any]]] = defaultdict(lambda: defaultdict(dict))
    order: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for row in snapshot_rows:
        schedule_date = row["schedule_date"]
        snapshot_id = int(row["snapshot_id"])
        states[schedule_date][snapshot_id][int(row["leg_id"])] = row["driver_id"]
        key = (row["created_at"], snapshot_id)
        if key not in order[schedule_date]:
            order[schedule_date].append(key)
    comparable = changes = 0
    for schedule_date, snapshot_order in order.items():
        ordered_ids = [item[1] for item in sorted(snapshot_order)]
        for previous_id, current_id in zip(ordered_ids, ordered_ids[1:]):
            previous = states[schedule_date][previous_id]
            current = states[schedule_date][current_id]
            for leg_id in set(previous) & set(current):
                comparable += 1
                changes += int(previous[leg_id] != current[leg_id])

    return {
        "leg_summary": leg_summary,
        "assignment_lead_samples": len(assignment_leads),
        "assignment_lead_median_hours": round(statistics.median(assignment_leads), 1) if assignment_leads else None,
        "assignment_lead_p25_hours": round(percentile(assignment_leads, 0.25) or 0, 1) if assignment_leads else None,
        "payment_summary": payment_summary,
        "audit_start": str(audit_min)[:10] if audit_min else None,
        "audit_end": str(audit_max)[:10] if audit_max else None,
        "staff_activity_start": str(staff_min)[:10] if staff_min else None,
        "communication_start": str(communications_min)[:10] if communications_min else None,
        "snapshot_comparable_assignments": comparable,
        "snapshot_assignment_changes": changes,
        "snapshot_change_rate": rate(changes, comparable),
    }


def analyze_flights(database: ReadOnlySQLite, start_date: date, cutoff: date) -> dict[str, Any]:
    linked_rows = database.rows(
        """
        SELECT l.id AS leg_id, l.pickup_location, l.dropoff_location,
               MAX(CASE WHEN f.id IS NOT NULL THEN 1 ELSE 0 END) AS has_flight,
               MAX(CASE WHEN f.scheduled_arrival_local IS NOT NULL OR f.scheduled_gate_arrival_local IS NOT NULL THEN 1 ELSE 0 END) AS has_scheduled,
               MAX(CASE WHEN f.estimated_arrival_local IS NOT NULL OR f.estimated_gate_arrival_local IS NOT NULL THEN 1 ELSE 0 END) AS has_estimated,
               MAX(CASE WHEN f.actual_arrival_local IS NOT NULL OR f.actual_gate_arrival_local IS NOT NULL THEN 1 ELSE 0 END) AS has_actual
        FROM reservations_leg l
        LEFT JOIN reservations_legflight lf ON lf.leg_id = l.id
        LEFT JOIN reservations_flight f ON f.id = COALESCE(lf.flight_id, l.flight_information_id)
        WHERE l.pickup_date BETWEEN ? AND ?
        GROUP BY l.id, l.pickup_location, l.dropoff_location
        """,
        [start_date.isoformat(), cutoff.isoformat()],
    )
    eligible = [
        row
        for row in linked_rows
        if classify_trip_type(row["pickup_location"], row["dropoff_location"]) == "arrival"
        or (
            classify_trip_type(row["pickup_location"], row["dropoff_location"]) == "cruise"
            and categorize_location(row["pickup_location"]) in {"MCO Terminal", "SFB Terminal"}
        )
    ]
    summary = {
        "arrival_legs": len(eligible),
        "linked_flight": sum(int(row["has_flight"] or 0) for row in eligible),
        "scheduled_time": sum(int(row["has_scheduled"] or 0) for row in eligible),
        "estimated_time": sum(int(row["has_estimated"] or 0) for row in eligible),
        "actual_time": sum(int(row["has_actual"] or 0) for row in eligible),
    }
    total = int(summary["arrival_legs"] or 0)
    for key in ("linked_flight", "scheduled_time", "estimated_time", "actual_time"):
        summary[f"{key}_rate"] = rate(int(summary[key] or 0), total)
    summary["blank_flight_type"] = database.scalar(
        "SELECT COUNT(*) FROM reservations_flight WHERE trim(COALESCE(flight_type, '')) = ''",
        default=0,
    )
    summary["total_flights"] = database.scalar("SELECT COUNT(*) FROM reservations_flight", default=0)
    return summary


def build_system_map() -> list[dict[str, Any]]:
    return [
        {
            "stage": "Lead and quote",
            "system_of_record": "Lead, Quote, FollowUpTask, LeadActivity",
            "primary_writes": "booking forms, staff views, GHL synchronization",
            "automation": "30-minute follow-up scheduler; hourly lost-lead and pre-pickup passes",
            "trust_risk": "message and sync logs exist, but definitions span several tables",
        },
        {
            "stage": "Booking",
            "system_of_record": "Customer, Reservation, Leg, LegStop, LegFlight",
            "primary_writes": "public booking, staff/admin entry, payment callbacks",
            "automation": "pricing, attribution, unpaid reminders, confirmation communications",
            "trust_risk": "manual and bulk admin paths can bypass event instrumentation",
        },
        {
            "stage": "Scheduling",
            "system_of_record": "Leg assignment plus drafts, snapshots, and driver windows",
            "primary_writes": "dispatcher board, auto-assignment, manual assignment",
            "automation": "static feasibility model, flight-aware floor, swap/gap/span passes",
            "trust_risk": "decision history is recent; current assignment fields are mutable",
        },
        {
            "stage": "Trip execution",
            "system_of_record": "Leg current status and LegStatus event history",
            "primary_writes": "driver portal, dispatcher board, bulk status tools",
            "automation": "reservation completion and selected route-metric refreshes",
            "trust_risk": "repeated events and bypass paths make timestamp meaning ambiguous",
        },
        {
            "stage": "Flight operations",
            "system_of_record": "Flight and LegFlight",
            "primary_writes": "AeroAPI refresh and staff edits",
            "automation": "tiered refresh every scheduler cycle; overnight confirmation sweep",
            "trust_risk": "latest state is retained, but forecast observations are overwritten",
        },
        {
            "stage": "Operational tasking",
            "system_of_record": "OperationalTask, CommunicationAttempt, EmailLog",
            "primary_writes": "task generator, staff actions, communications",
            "automation": "task scan each 30-minute scheduler cycle",
            "trust_risk": "staff activity and communication history begin recently",
        },
        {
            "stage": "Payment and payout",
            "system_of_record": "Payment, Reservation payment fields, LegPayment, DriverPayment",
            "primary_writes": "Stripe callbacks and staff payout workflows",
            "automation": "reservation reconciliation and unpaid reminder policies",
            "trust_risk": "duplicated summary fields require reconciliation to transactions",
        },
        {
            "stage": "Fleet telemetry",
            "system_of_record": "latest Samsara fields on FleetVehicle",
            "primary_writes": "read-only Samsara polling",
            "automation": "separate in-process poller",
            "trust_risk": "latest position only; no historical trajectory table is populated",
        },
    ]


def build_trust_matrix(
    inventory: list[dict[str, Any]],
    status: dict[str, Any],
    route_metrics: dict[str, Any],
    operations: dict[str, Any],
    flights: dict[str, Any],
) -> list[dict[str, Any]]:
    counts = {row["table"]: row["rows"] for row in inventory}
    instrumentation = status["instrumentation_start"].isoformat()
    return [
        {
            "data_asset": "Reservations and booked legs",
            "grain": "one reservation / one service leg",
            "coverage": f"{format_number(counts.get('reservations_reservation'))} reservations; {format_number(counts.get('reservations_leg'))} legs",
            "trust": "Conditional",
            "safe_use": "booked demand and service mix after excluding invalid dates/statuses",
            "limitation": "mutable operational state and date outliers",
        },
        {
            "data_asset": "Status history",
            "grain": "one status event per leg transition attempt",
            "coverage": f"usable cohort begins {instrumentation}; {format_number(counts.get('reservations_legstatus'))} events",
            "trust": "Conditional",
            "safe_use": "duration distributions after chain and timing validation",
            "limitation": "repeats, late backfills, and write-path bypasses",
        },
        {
            "data_asset": "Flights",
            "grain": "latest known state per flight",
            "coverage": f"{format_number(flights['total_flights'])} flights; actual-time coverage {format_percent(flights['actual_time_rate'])} in cohort",
            "trust": "Conditional",
            "safe_use": "current flight operations and final actual-time availability",
            "limitation": "no historical forecast snapshots for prediction-at-decision-time",
        },
        {
            "data_asset": "Schedule drafts and snapshots",
            "grain": "schedule version and leg assignment",
            "coverage": f"{format_number(counts.get('reservations_schedulesnapshot'))} snapshots; {format_number(counts.get('reservations_scheduledraft'))} drafts",
            "trust": "Conditional",
            "safe_use": "recent schedule-change and publish-workflow analysis",
            "limitation": "short history and mutable pre-instrumentation decisions",
        },
        {
            "data_asset": "Audit log",
            "grain": "selected model/field action",
            "coverage": f"{operations['audit_start'] or 'none'} to {operations['audit_end'] or 'none'}",
            "trust": "Unreliable historically",
            "safe_use": "recent workflow spot checks only",
            "limitation": "recent start and incomplete write-path coverage",
        },
        {
            "data_asset": "Route timing metrics",
            "grain": "trip/route/time/day bucket",
            "coverage": f"{route_metrics['total_buckets']} buckets; {route_metrics['reliable_buckets']} have at least 5 samples",
            "trust": "Conditional",
            "safe_use": "display estimates for sufficiently sampled, recently refreshed buckets",
            "limitation": "sparse/stale buckets and incomplete refresh triggers",
        },
        {
            "data_asset": "Payments and payouts",
            "grain": "customer transaction, reservation summary, and leg payout",
            "coverage": f"{format_number(counts.get('payment_payment'))} customer payments; {format_number(counts.get('drivers_legpayment'))} leg payments",
            "trust": "Conditional",
            "safe_use": "reconciliation when transaction records control summary fields",
            "limitation": "multiple representations of paid and refunded amounts",
        },
        {
            "data_asset": "Staff and communications activity",
            "grain": "staff action or communication attempt",
            "coverage": f"activity since {operations['staff_activity_start'] or 'none'}; communications since {operations['communication_start'] or 'none'}",
            "trust": "Conditional",
            "safe_use": "recent workload and workflow adoption analysis",
            "limitation": "not a historical productivity series",
        },
        {
            "data_asset": "Driver capacity and demand aggregates",
            "grain": "driver-day / service-hour",
            "coverage": f"{format_number(counts.get('reservations_driverdailycapacity'))} capacity rows; {format_number(counts.get('reservations_demandpattern'))} demand rows",
            "trust": "Missing",
            "safe_use": "none until population and freshness checks exist",
            "limitation": "implemented models are not populated",
        },
        {
            "data_asset": "Historical GPS trajectories",
            "grain": "driver/vehicle position observation",
            "coverage": f"{format_number(counts.get('reservations_driverlocation'))} driver-location rows",
            "trust": "Missing",
            "safe_use": "latest vehicle visibility only via FleetVehicle fields",
            "limitation": "no historical route, dwell, or deadhead reconstruction",
        },
    ]


def build_quality_findings(
    profile: dict[str, Any],
    dates: dict[str, Any],
    status: dict[str, Any],
    route_metrics: dict[str, Any],
    inventory: list[dict[str, Any]],
    operations: dict[str, Any],
) -> list[dict[str, Any]]:
    counts = {row["table"]: row["rows"] for row in inventory}
    findings = []
    if not profile["fresh_snapshot_present"]:
        findings.append(
            {
                "severity": "Critical",
                "confidence": "High",
                "finding": "The required fresh production snapshot is absent",
                "evidence": f"Analysis used {profile['selected_path']} as a preliminary working copy",
                "analytical_risk": "headline values cannot be certified as production-current",
                "recommended_fix": "place an immutable export at the requested path and rerun the notebook",
            }
        )
    findings.extend(
        [
            {
                "severity": "High",
                "confidence": "High",
                "finding": "Completed-leg event history is incomplete outside the instrumented cohort",
                "evidence": f"cohort begins {status['instrumentation_start']}; timely completion-event rate is {format_percent(status['cohort_timely_completion_rate'])}",
                "analytical_risk": "historic on-time and duration trends would be biased",
                "recommended_fix": "publish metric eligibility windows and backfill only with explicit provenance",
            },
            {
                "severity": "High",
                "confidence": "High",
                "finding": "Status events can repeat and earliest/latest semantics can disagree",
                "evidence": f"{format_number(status['legs_with_repeated_status'])} completed legs contain repeats; {format_number(status['semantic_disagreement'])} change validity by semantic choice",
                "analytical_risk": "route and service-duration metrics can change based on ORM ordering",
                "recommended_fix": "define canonical occurred_at semantics and make transitions idempotent",
            },
            {
                "severity": "High",
                "confidence": "High",
                "finding": "Bulk completion bypasses status-event creation",
                "evidence": "reservation and leg admin actions use bulk update while driver/dispatcher paths create LegStatus rows",
                "analytical_risk": "current status and event history diverge by workflow",
                "recommended_fix": "route all transitions through one transactional state-change service",
            },
            {
                "severity": "High",
                "confidence": "High",
                "finding": "Route metrics are sparse, stale, and refreshed from selected paths only",
                "evidence": f"{route_metrics['reliable_buckets']} of {route_metrics['total_buckets']} buckets have at least 5 samples; {route_metrics['stale_buckets_14d']} are more than 14 days stale at cutoff",
                "analytical_risk": "display estimates vary in quality and silently fall back to static assumptions",
                "recommended_fix": "refresh from every completion path and enforce sample/freshness gates",
            },
            {
                "severity": "High",
                "confidence": "High",
                "finding": "Flight forecasts are overwritten rather than snapshotted",
                "evidence": "Flight stores the latest scheduled, estimated, and actual times but no observation history",
                "analytical_risk": "delay accuracy at assignment time and leakage-safe models cannot be reconstructed",
                "recommended_fix": "append immutable forecast observations with observed_at and provider metadata",
            },
            {
                "severity": "High",
                "confidence": "High",
                "finding": "Capacity, demand, and GPS analytical tables are unpopulated",
                "evidence": f"capacity={counts.get('reservations_driverdailycapacity', 0)}, demand={counts.get('reservations_demandpattern', 0)}, driver locations={counts.get('reservations_driverlocation', 0)}",
                "analytical_risk": "utilization, supply/demand, deadhead, and route-path claims are not directly measurable",
                "recommended_fix": "populate governed aggregates and retain consented telemetry only for defined uses",
            },
            {
                "severity": "Medium",
                "confidence": "High",
                "finding": "Pickup dates contain implausible outliers",
                "evidence": f"{format_number(dates['invalid_pickup_dates'])} legs fall outside 2015-{date.fromisoformat(profile['analysis_cutoff']).year + 2}; maximum is {dates['max_pickup_date']}",
                "analytical_risk": "unbounded queries and time series produce misleading ranges",
                "recommended_fix": "validate service dates on write and quarantine existing outliers",
            },
            {
                "severity": "Medium",
                "confidence": "High",
                "finding": "Schedule and staff audit coverage is recent",
                "evidence": f"audit starts {operations['audit_start'] or 'not available'}; staff activity starts {operations['staff_activity_start'] or 'not available'}",
                "analytical_risk": "historical schedule churn and staff productivity cannot be compared consistently",
                "recommended_fix": "define retention and event coverage SLAs before scorecard use",
            },
        ]
    )
    severity_order = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    return sorted(findings, key=lambda item: (severity_order[item["severity"]], item["finding"]))


def build_kpi_framework() -> list[dict[str, Any]]:
    return [
        {
            "role": "Primary KPI",
            "metric": "On-time pickup rate",
            "definition": "eligible legs with a verified actual pickup timestamp inside the agreed trip-type SLA divided by eligible legs",
            "cadence": "weekly with daily exception review",
            "current_readiness": "Not decision-ready",
            "required_work": "define pickup semantics/SLA; make status events idempotent and source-aware",
        },
        {
            "role": "Primary KPI",
            "metric": "In-house coverage rate",
            "definition": "eligible service legs assigned to active in-house drivers by the dispatch cutoff divided by eligible scheduled legs",
            "cadence": "daily and weekly",
            "current_readiness": "Conditional",
            "required_work": "define dispatch cutoff and distinguish intentional affiliates from reactive farm-outs",
        },
        {
            "role": "Primary KPI",
            "metric": "Published-schedule reliability",
            "definition": "published legs whose driver and vehicle remain unchanged through pickup divided by published eligible legs",
            "cadence": "weekly",
            "current_readiness": "Recent cohorts only",
            "required_work": "make publish snapshots and reassignment reason codes complete",
        },
        {
            "role": "Driver",
            "metric": "Valid status-chain coverage",
            "definition": "completed eligible legs with ordered on-way, picked-up, and completed events inside duration bounds",
            "cadence": "daily quality control",
            "current_readiness": "Conditional",
            "required_work": "standardize first/last/reversal semantics",
        },
        {
            "role": "Driver",
            "metric": "Assignment lead time",
            "definition": "hours from the canonical first committed driver assignment to scheduled pickup",
            "cadence": "weekly by trip type",
            "current_readiness": "Proxy only",
            "required_work": "retain first assignment separately from latest assignment",
        },
        {
            "role": "Driver",
            "metric": "Clear-time prediction error",
            "definition": "absolute difference between predicted and verified actual clear time on eligible legs",
            "cadence": "weekly by route/time bucket",
            "current_readiness": "Prototype-ready cohort",
            "required_work": "version predictions and retain the value used at scheduling time",
        },
        {
            "role": "Guardrail",
            "metric": "Overlong duty-day rate",
            "definition": "driver-days above the agreed raw/effective span threshold divided by worked driver-days",
            "cadence": "daily and weekly",
            "current_readiness": "Conditional",
            "required_work": "confirm actual duty boundaries and break-credit policy",
        },
        {
            "role": "Guardrail",
            "metric": "Near-term uncovered-leg rate",
            "definition": "eligible legs unassigned inside the agreed hours-to-pickup threshold divided by eligible near-term legs",
            "cadence": "continuous operations alert",
            "current_readiness": "Conditional",
            "required_work": "define intentional affiliate/unassigned exclusions",
        },
    ]


def build_predictive_matrix(
    route_baseline: dict[str, Any], demand_baseline: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        {
            "candidate": "Flight delay risk at dispatch time",
            "label_quality": "Missing historical forecast snapshots",
            "prototype_result": "Blocked",
            "decision": "Do not model yet",
            "next_gate": "retain provider observations and assignment-time features",
        },
        {
            "candidate": "Pickup delay risk",
            "label_quality": "Pickup event meaning and SLA are not canonical",
            "prototype_result": "Blocked",
            "decision": "Do not automate",
            "next_gate": "define actual pickup and capture transition source/reversal",
        },
        {
            "candidate": "Drive-duration baseline",
            "label_quality": f"{format_number(route_baseline['eligible_observations'])} eligible in-house status chains",
            "prototype_result": f"historical-route MAE {format_number(route_baseline['historical_median_mae_minutes'], 1)} min vs static {format_number(route_baseline['static_mae_minutes'], 1)} min on {format_number(route_baseline['test_observations'])} held-out legs",
            "decision": "Shadow-test only",
            "next_gate": "fresh snapshot, route normalization, complete write-path refresh",
        },
        {
            "candidate": "Airport dwell baseline",
            "label_quality": f"{format_number(route_baseline['dwell_samples'])} arrival legs with on-location to picked-up events",
            "prototype_result": f"median {format_number(route_baseline['dwell_median_minutes'], 1)} min; p75 {format_number(route_baseline['dwell_p75_minutes'], 1)} min",
            "decision": "Interpret as a workflow proxy",
            "next_gate": "confirm what on-location means operationally",
        },
        {
            "candidate": "Daily demand forecast",
            "label_quality": f"{format_number(demand_baseline['daily_rows'])} service days from eligible cohort",
            "prototype_result": f"weekday-median MAE {format_number(demand_baseline['weekday_median_mae_legs'], 2)} legs vs overall-median {format_number(demand_baseline['overall_median_mae_legs'], 2)}",
            "decision": "Use as planning baseline after fresh rerun",
            "next_gate": "add booking-as-of snapshots for lead-time-aware forecasts",
        },
        {
            "candidate": "Chained-trip feasibility",
            "label_quality": "Status chains exist but lateness and decision-time inputs are incomplete",
            "prototype_result": "Conditional",
            "decision": "Backtest rules, not production automation",
            "next_gate": "version clear-time predictions and define lateness outcomes",
        },
        {
            "candidate": "Farm-out risk",
            "label_quality": "Affiliate assignment is only a proxy for reactive farm-out",
            "prototype_result": "Conditional",
            "decision": "Do not train on current label",
            "next_gate": "capture assignment reason and decision timestamp",
        },
    ]


def build_roadmap(database: ReadOnlySQLite) -> list[dict[str, Any]]:
    details = {
        "Canonical transition service": ("Unify every leg/reservation status change and emit one idempotent event.", "Current status and history diverge across driver, dispatcher, and admin paths.", "Reliable service timing and fewer workflow-specific defects.", "Application transaction boundary and admin refactor.", "Migration must preserve current staff workflows; add audit-only rollout first."),
        "Immutable operational event contract": ("Store occurred_at, recorded_at, source, actor, reason, reversal, and idempotency key.", "Repeated events have ambiguous first-versus-last meaning.", "Stable labels for KPIs and models.", "Canonical transition service and event taxonomy.", "Avoid rewriting history; corrections must be additive."),
        "Flight forecast observation history": ("Append each provider observation instead of overwriting the latest state.", "Decision-time delay information cannot be reconstructed.", "Enables honest arrival-risk and staffing analysis.", "AeroAPI refresh path, retention policy, provider cost controls.", "Bound refresh cadence and retain provider timestamps."),
        "Schedule decision snapshots and reason codes": ("Snapshot publish state and every material reassignment with reason and decision time.", "Schedule churn and reactive farm-outs are only partially observable.", "Measures plan stability and improves dispatcher decision support.", "Draft/snapshot workflow and assignment service.", "Keep snapshots compact and distinguish preview from commitment."),
        "Nightly data-quality gates": ("Monitor invalid dates/enums, event completeness, freshness, joins, and empty derived tables.", "Known defects can silently enter reporting cohorts.", "Prevents misleading scorecards and catches instrumentation regressions quickly.", "Stable metric eligibility rules and alert owner.", "Use rate-based, late-arrival-aware thresholds."),
        "Governed metric refresh pipeline": ("Refresh route, capacity, and demand aggregates from all completion paths with freshness metadata.", "Existing models are sparse, stale, or empty.", "Creates a consistent source for planning and monitoring.", "Canonical events and scheduled batch execution.", "Do not serve low-sample buckets without fallback labels."),
        "Weekly operations scorecard": ("Publish three primary KPIs with drivers, guardrails, cohorts, and owners.", "Leadership lacks a stable operating view.", "Faster exception management and trend accountability.", "At least four weeks of stable instrumentation.", "No targets until definitions and baselines are validated."),
        "Payment reconciliation checks": ("Reconcile transaction rows to reservation summaries and leg payouts daily.", "Multiple paid/refunded representations can drift.", "Reduces manual review and protects margin reporting.", "Canonical transaction precedence and exception ownership.", "Never auto-correct money without review and audit."),
        "Duration and demand shadow baselines": ("Version simple median/weekday baselines and compare them with current rules offline.", "Static timing and staffing assumptions lack recurring accuracy measurement.", "Improves forecasts without premature model complexity.", "Fresh eligible cohorts and stored prediction-as-of values.", "Shadow only until calibration and segment stability pass."),
        "Dispatcher exception decision support": ("Surface predicted clear time, uncertainty, coverage, and reason-coded override.", "Dispatchers cannot see confidence or learn systematically from overrides.", "Better tight-turn and farm-out choices while preserving human control.", "Validated baselines, UI design, alert-fatigue review.", "Never hide static fallback or force an assignment."),
        "Production predictive automation": ("Automate only bounded decisions after shadow tests and operational sign-off.", "Current labels are not strong enough for autonomous action.", "Potential long-run efficiency once evidence is reliable.", "All prior phases, monitoring, rollback, and owner approval.", "Do not automate pickup-delay, farm-out, or chain decisions yet."),
    }
    output = []
    for row in database.rows(ROADMAP_PRIORITY_SQL):
        title = row["recommendation"]
        why, problem, expected, dependencies, risks = details[title]
        row.update(
            {
                "why": why,
                "problem": problem,
                "expected_impact": expected,
                "complexity": {2: "Low", 3: "Medium", 4: "High", 5: "Very high"}.get(row["effort"], "Medium"),
                "dependencies": dependencies,
                "risks_and_guardrails": risks,
            }
        )
        output.append(row)
    return output


def build_validation_questions() -> list[dict[str, Any]]:
    questions = [
        (1, "When a driver records the same status more than once, should the first valid event or the last correction represent actual time?", "Status-chain semantics and all duration metrics"),
        (2, "What exactly does pickup_time mean for arrivals, departures, cruise trips, and point-to-point work?", "On-time pickup and dwell definitions"),
        (3, "Does driver_assigned_at represent the first commitment, the latest assignment, or both depending on workflow?", "Assignment lead time and churn"),
        (4, "Which affiliate assignments are planned partnerships versus last-minute farm-outs?", "In-house coverage and farm-out labels"),
        (5, "How often are bulk admin completion actions used, and in which operating scenarios?", "Magnitude of missing status history"),
        (6, "Which schedule state is the operational commitment: draft submission, review, publish, notification, or another moment?", "Published-schedule reliability"),
        (7, "What lateness thresholds should define an on-time pickup by trip type?", "Primary KPI and prediction labels"),
        (8, "Which route, store-stop, terminal, traffic, or handoff exceptions most often invalidate static timing?", "Route segmentation and scheduler guardrails"),
    ]
    return [
        {"priority": priority, "question": question, "decision_affected": affected, "status": "Awaiting operations validation"}
        for priority, question, affected in questions
    ]


def build_chart_map() -> list[dict[str, Any]]:
    return [
        {"section": "Event coverage", "question": "When did status history become usable?", "family": "Trend", "chart": "multi-series line", "fields": "month, metric, rate", "claim": "coverage improves materially only in recent cohorts", "palette": "hard two-root cap"},
        {"section": "Timing readiness", "question": "How many route buckets are decision-ready?", "family": "Comparison", "chart": "bar", "fields": "confidence_bucket, route_buckets", "claim": "most buckets are thin", "palette": "single-root preferred"},
        {"section": "Demand", "question": "What booked service volume is visible over time?", "family": "Trend", "chart": "line", "fields": "month, scheduled_legs", "claim": "volume context for planning baselines", "palette": "single-root preferred"},
        {"section": "Roadmap", "question": "Which recommendations combine high impact with manageable effort?", "family": "Ranking", "chart": "bar", "fields": "recommendation, priority_score", "claim": "instrumentation work precedes automation", "palette": "single-root preferred"},
    ]


def build_artifact(results: dict[str, Any], generated_at: str) -> dict[str, Any]:
    profile = results["source_profile"]
    status = results["status_history"]
    route_metrics = results["route_metrics"]
    route_baseline = results["route_baseline"]
    demand = results["demand_baseline"]
    operations = results["operations"]
    findings = results["quality_findings"]
    snapshot_status = "ready" if profile["fresh_snapshot_present"] and profile["quick_check"] == "ok" else "partial"
    validation_rating = "Share with caveats" if profile["fresh_snapshot_present"] else "Needs revision"

    chain_trend = []
    for row in status["monthly"]:
        if parse_date(row["month"]) < status["instrumentation_start"]:
            continue
        for metric, label in (
            ("timely_completion_event_rate", "Timely completion event"),
            ("earliest_valid_chain_rate", "Valid first-event chain"),
            ("latest_valid_chain_rate", "Valid last-event chain"),
        ):
            chain_trend.append(
                {
                    "month": row["month"],
                    "month_label": row["month_label"],
                    "metric": label,
                    "rate": row[metric],
                    "completed_legs": row["completed_legs"],
                }
            )

    summary = [{
        "reservations": next((item["rows"] for item in results["table_inventory"] if item["table"] == "reservations_reservation"), 0),
        "legs": next((item["rows"] for item in results["table_inventory"] if item["table"] == "reservations_leg"), 0),
        "cohort_chain_rate": status["cohort_chain_rate"],
        "reliable_route_buckets": route_metrics["reliable_buckets"],
        "total_route_buckets": route_metrics["total_buckets"],
        "invalid_pickup_dates": results["dates_and_categories"]["invalid_pickup_dates"],
    }]

    source_label = "Fresh production SQLite snapshot" if profile["fresh_snapshot_present"] else "Preliminary local SQLite working copy"
    sources = [
        {
            "id": "snapshot_inventory",
            "label": source_label,
            "path": profile["selected_path"],
            "query": {
                "engine": "sqlite",
                "sql": "SELECT name, type FROM sqlite_master WHERE type = 'table' ORDER BY name",
                "description": "Read-only inventory and aggregate profiling of the operational SQLite source.",
                "executed_at": generated_at,
                "tables_used": list(CORE_TABLES.values()),
            },
        },
        {
            "id": "status_history_sql",
            "label": "Leg execution status reconstruction",
            "path": "docs/operations-intelligence/operations_intelligence_audit.ipynb",
            "query": {
                "engine": "sqlite",
                "sql": STATUS_ANALYSIS_SQL,
                "description": "Reconstructs first and last status events at one row per leg; calculations are in the notebook.",
                "executed_at": generated_at,
                "tables_used": ["reservations_leg", "reservations_reservation", "reservations_legstatus"],
            },
        },
        {
            "id": "route_metric_sql",
            "label": "Route timing metric profile",
            "path": "docs/operations-intelligence/operations_intelligence_audit.ipynb",
            "query": {
                "engine": "sqlite",
                "sql": ROUTE_METRIC_SQL,
                "description": "Profiles route timing buckets, sample sizes, missing values, and freshness.",
                "executed_at": generated_at,
                "tables_used": ["reservations_routetimingmetric"],
            },
        },
        {
            "id": "roadmap_scoring_sql",
            "label": "Roadmap impact-effort scoring",
            "path": "docs/operations-intelligence/analysis.py",
            "query": {
                "engine": "sqlite",
                "sql": ROADMAP_PRIORITY_SQL,
                "description": "Scores the explicitly documented roadmap inputs as two times impact minus effort; phase controls final sequence.",
                "executed_at": generated_at,
                "tables_used": [],
            },
        },
        {
            "id": "analysis_code",
            "label": "Reproducible operational analysis",
            "path": "docs/operations-intelligence/analysis.py",
        },
        {
            "id": "scheduler_code",
            "label": "Scheduling and feasibility implementation",
            "path": "dispatching/scheduler.py",
        },
        {
            "id": "workflow_code",
            "label": "Operational write paths",
            "path": "reservations/admin.py",
        },
    ]

    manifest_sources = [
        {"id": item["id"], "label": item["label"], "path": item["path"]}
        for item in sources
    ]
    cards = [
        {
            "id": "legs_card",
            "description": "Service legs present in the selected source.",
            "dataset": "summary",
            "sourceId": "snapshot_inventory",
            "metrics": [{"label": "Service legs", "field": "legs", "format": "number"}],
        },
        {
            "id": "chain_card",
            "description": f"Completed legs since {status['instrumentation_start']} with a valid first-event status chain.",
            "dataset": "summary",
            "sourceId": "status_history_sql",
            "metrics": [{"label": "Valid status-chain coverage", "field": "cohort_chain_rate", "format": "percent"}],
        },
        {
            "id": "route_card",
            "description": "Route timing buckets with at least five usable observations.",
            "dataset": "summary",
            "sourceId": "route_metric_sql",
            "metrics": [
                {"label": "Usable route buckets", "field": "reliable_route_buckets", "format": "number"},
                {"label": "All route buckets", "field": "total_route_buckets", "format": "number"},
            ],
        },
        {
            "id": "date_card",
            "description": "Legs with pickup years outside the bounded validity window.",
            "dataset": "summary",
            "sourceId": "snapshot_inventory",
            "metrics": [{"label": "Invalid pickup dates", "field": "invalid_pickup_dates", "format": "number"}],
        },
    ]

    charts = [
        {
            "id": "chain_coverage_chart",
            "title": "Status-event coverage by service month",
            "subtitle": f"Completed legs from {status['instrumentation_start']} through {profile['analysis_cutoff']}; monthly cohort size is available in tooltips.",
            "type": "line",
            "dataset": "chain_trend",
            "sourceId": "status_history_sql",
            "encodings": {
                "x": {"field": "month", "type": "temporal", "label": "Service month"},
                "y": {"field": "rate", "type": "quantitative", "label": "Coverage rate", "format": "percent"},
                "color": {"field": "metric", "type": "nominal", "label": "Metric"},
                "tooltip": [{"field": "completed_legs", "type": "quantitative", "label": "Completed legs"}],
            },
            "valueFormat": "percent",
            "layout": "full",
        },
        {
            "id": "route_confidence_chart",
            "title": "Route timing buckets by sample depth",
            "subtitle": f"{route_metrics['total_buckets']} derived buckets as of {profile['analysis_cutoff']}; scheduler use starts at five samples.",
            "type": "bar",
            "dataset": "route_confidence",
            "sourceId": "route_metric_sql",
            "encodings": {
                "x": {"field": "confidence_bucket", "type": "ordinal", "label": "Samples per bucket"},
                "y": {"field": "route_buckets", "type": "quantitative", "label": "Route buckets"},
                "tooltip": [{"field": "underlying_samples", "type": "quantitative", "label": "Underlying samples"}],
            },
            "valueFormat": "number",
            "layout": "full",
        },
        {
            "id": "monthly_volume_chart",
            "title": "Booked service legs by month",
            "subtitle": "Last 18 months through the analysis cutoff; cancelled records excluded and current month may be incomplete.",
            "type": "line",
            "dataset": "monthly_volume",
            "sourceId": "snapshot_inventory",
            "encodings": {
                "x": {"field": "month", "type": "temporal", "label": "Service month"},
                "y": {"field": "scheduled_legs", "type": "quantitative", "label": "Booked legs"},
            },
            "valueFormat": "number",
            "layout": "full",
        },
        {
            "id": "roadmap_chart",
            "title": "Roadmap priority score",
            "subtitle": "Impact is weighted twice and reduced by implementation effort; phase still controls sequence.",
            "type": "bar",
            "dataset": "roadmap",
            "sourceId": "roadmap_scoring_sql",
            "encodings": {
                "x": {"field": "recommendation", "type": "nominal", "label": "Recommendation"},
                "y": {"field": "priority_score", "type": "quantitative", "label": "Priority score"},
                "tooltip": [
                    {"field": "phase", "type": "quantitative", "label": "Phase"},
                    {"field": "confidence", "type": "nominal", "label": "Confidence"},
                ],
            },
            "valueFormat": "number",
            "layout": "full",
        },
    ]

    tables = [
        {
            "id": "system_map_table",
            "title": "Operational system map",
            "subtitle": "Current systems of record, write paths, automation, and trust risks.",
            "dataset": "system_map",
            "sourceId": "scheduler_code",
            "defaultSort": {"field": "stage", "direction": "asc"},
            "columns": [
                {"field": "stage", "label": "Stage", "type": "text"},
                {"field": "system_of_record", "label": "System of record", "type": "text"},
                {"field": "primary_writes", "label": "Primary writes", "type": "text"},
                {"field": "automation", "label": "Automation", "type": "text"},
                {"field": "trust_risk", "label": "Trust risk", "type": "text"},
            ],
        },
        {
            "id": "trust_table",
            "title": "Data trust matrix",
            "subtitle": "Safe analytical uses and limitations for the current source.",
            "dataset": "trust_matrix",
            "sourceId": "analysis_code",
            "defaultSort": {"field": "data_asset", "direction": "asc"},
            "columns": [
                {"field": "data_asset", "label": "Data asset", "type": "text"},
                {"field": "grain", "label": "Grain", "type": "text"},
                {"field": "coverage", "label": "Coverage", "type": "text"},
                {"field": "trust", "label": "Trust", "type": "text"},
                {"field": "safe_use", "label": "Safe use", "type": "text"},
                {"field": "limitation", "label": "Limitation", "type": "text"},
            ],
        },
        {
            "id": "quality_table",
            "title": "Material data-quality findings",
            "subtitle": "Issues ordered by decision risk; counts and dates are source-specific.",
            "dataset": "quality_findings",
            "sourceId": "analysis_code",
            "defaultSort": {"field": "severity", "direction": "asc"},
            "columns": [
                {"field": "severity", "label": "Severity", "type": "text"},
                {"field": "finding", "label": "Finding", "type": "text"},
                {"field": "evidence", "label": "Evidence", "type": "text"},
                {"field": "analytical_risk", "label": "Why it matters", "type": "text"},
                {"field": "confidence", "label": "Confidence", "type": "text"},
                {"field": "recommended_fix", "label": "Recommended fix", "type": "text"},
            ],
        },
        {
            "id": "route_table",
            "title": "Observed drive-time segments",
            "subtitle": "Eligible in-house completed legs; first picked-up to first completed, minimum 10 samples per row.",
            "dataset": "route_summary",
            "sourceId": "status_history_sql",
            "defaultSort": {"field": "samples", "direction": "desc"},
            "columns": [
                {"field": "route", "label": "Route", "type": "text"},
                {"field": "trip_type", "label": "Trip type", "type": "text"},
                {"field": "samples", "label": "Samples", "format": "number"},
                {"field": "observed_median_minutes", "label": "Median min", "format": "number"},
                {"field": "observed_p75_minutes", "label": "P75 min", "format": "number"},
                {"field": "static_minutes", "label": "Static min", "format": "number"},
                {"field": "median_minus_static", "label": "Median - static", "format": "number", "movement": True},
            ],
        },
        {
            "id": "kpi_table",
            "title": "Recommended operating KPI framework",
            "subtitle": "Definitions precede targets; readiness reflects current instrumentation.",
            "dataset": "kpi_framework",
            "sourceId": "analysis_code",
            "defaultSort": {"field": "role", "direction": "asc"},
            "columns": [
                {"field": "role", "label": "Role", "type": "text"},
                {"field": "metric", "label": "Metric", "type": "text"},
                {"field": "definition", "label": "Definition", "type": "text"},
                {"field": "cadence", "label": "Cadence", "type": "text"},
                {"field": "current_readiness", "label": "Readiness", "type": "text"},
                {"field": "required_work", "label": "Required work", "type": "text"},
            ],
        },
        {
            "id": "predictive_table",
            "title": "Predictive feature readiness",
            "subtitle": "Chronological baseline results are preliminary until the fresh snapshot rerun.",
            "dataset": "predictive_matrix",
            "sourceId": "analysis_code",
            "defaultSort": {"field": "candidate", "direction": "asc"},
            "columns": [
                {"field": "candidate", "label": "Candidate", "type": "text"},
                {"field": "label_quality", "label": "Label quality", "type": "text"},
                {"field": "prototype_result", "label": "Prototype result", "type": "text"},
                {"field": "decision", "label": "Decision", "type": "text"},
                {"field": "next_gate", "label": "Next gate", "type": "text"},
            ],
        },
        {
            "id": "roadmap_table",
            "title": "Prioritized operations intelligence roadmap",
            "subtitle": "Phase controls sequencing; score = 2 x impact - effort.",
            "dataset": "roadmap",
            "sourceId": "roadmap_scoring_sql",
            "defaultSort": {"field": "phase", "direction": "asc"},
            "columns": [
                {"field": "phase", "label": "Phase", "format": "number"},
                {"field": "recommendation", "label": "Recommendation", "type": "text"},
                {"field": "impact", "label": "Impact", "format": "number"},
                {"field": "effort", "label": "Effort", "format": "number"},
                {"field": "priority_score", "label": "Score", "format": "number"},
                {"field": "confidence", "label": "Confidence", "type": "text"},
                {"field": "why", "label": "Why", "type": "text"},
                {"field": "problem", "label": "Problem", "type": "text"},
                {"field": "expected_impact", "label": "Expected impact", "type": "text"},
                {"field": "complexity", "label": "Complexity", "type": "text"},
                {"field": "dependencies", "label": "Dependencies", "type": "text"},
                {"field": "risks_and_guardrails", "label": "Risks / guardrails", "type": "text"},
            ],
            "density": "dense",
        },
        {
            "id": "validation_questions_table",
            "title": "Targeted operations validation questions",
            "subtitle": "Eight unresolved questions that materially affect definitions or recommendations.",
            "dataset": "validation_questions",
            "sourceId": "analysis_code",
            "defaultSort": {"field": "priority", "direction": "asc"},
            "columns": [
                {"field": "priority", "label": "Priority", "format": "number"},
                {"field": "question", "label": "Question", "type": "text"},
                {"field": "decision_affected", "label": "Decision affected", "type": "text"},
                {"field": "status", "label": "Status", "type": "text"},
            ],
        },
    ]

    route_comparison = (
        f"A leakage-safe chronological holdout includes **{format_number(route_baseline['test_observations'])}** legs. "
        f"The current static route table has mean absolute error **{format_number(route_baseline['static_mae_minutes'], 1)} minutes**; "
        f"a simple historical route median has **{format_number(route_baseline['historical_median_mae_minutes'], 1)} minutes**. "
        "This is a baseline diagnostic—not evidence to change production scheduling—because the source is preliminary and status semantics remain conditional."
    )
    demand_comparison = (
        f"A weekday-median demand baseline was evaluated on **{format_number(demand['test_days'])} held-out service days**. "
        f"Its error is **{format_number(demand['weekday_median_mae_legs'], 2)} legs/day**, compared with "
        f"**{format_number(demand['overall_median_mae_legs'], 2)}** for an overall-median baseline. "
        "Booking-as-of snapshots are still required before staffing forecasts can incorporate lead time."
    )

    blocks = [
        {"id": "title", "type": "markdown", "body": "# Grayson Towncar Operations Intelligence"},
        {
            "id": "executive_summary",
            "type": "markdown",
            "body": (
                "## Executive Summary\n\n"
                f"- **Build the data foundation before automating dispatch.** The package rates this analysis **{validation_rating.lower()}** because the requested fresh production snapshot was not available; it uses a preliminary working copy and must be rerun before operational decisions.\n"
                f"- **Recent trip-execution data is useful only inside a bounded cohort.** The eligible status-history period begins **{status['instrumentation_start']}**, and valid first-event status-chain coverage is **{format_percent(status['cohort_chain_rate'])}** across **{format_number(status['cohort_completed_legs'])}** completed legs.\n"
                f"- **Current scheduling intelligence mixes explicit static rules with thin derived history.** Only **{route_metrics['reliable_buckets']} of {route_metrics['total_buckets']}** route buckets have at least five samples, while chain feasibility intentionally uses the static model.\n"
                "- **The highest-value sequence is instrumentation, governed scorecards, shadow analytics, then bounded automation.** Flight-delay, pickup-delay, and farm-out models should not move to production with current labels."
            ),
        },
        {"id": "headline_metrics", "type": "metric-strip", "cardIds": ["legs_card", "chain_card", "route_card", "date_card"]},
        {
            "id": "source_fingerprint",
            "type": "markdown",
            "sourceId": "snapshot_inventory",
            "body": (
                "## Source trust and reproducibility\n\n"
                f"- **Selected source:** `{profile['selected_path']}` ({profile['source_tier']}); requested snapshot present: **{profile['fresh_snapshot_present']}**.\n"
                f"- **SHA-256:** `{profile['sha256']}`; file modified UTC: **{profile['modified_utc']}**. The source does not encode a separate export timestamp.\n"
                f"- **Integrity and schema:** SQLite quick-check **{profile['quick_check']}**; **{profile['table_count']}** tables; **{profile['migration_count']}** recorded migrations; latest applied **{profile['latest_migration']}**.\n"
                f"- **Analysis contract:** cutoff **{profile['analysis_cutoff']}** in **{profile['timezone']}**; the database was opened with SQLite `mode=ro` and `PRAGMA query_only=ON`."
            ),
        },
        {
            "id": "system_heading",
            "type": "markdown",
            "body": "## The operation is well represented, but its history is uneven\n\nThe application covers the full customer and operating lifecycle. The weakness is not a lack of tables; it is inconsistent event creation, recent-only decision history, and derived datasets that are not continuously populated. The map below separates live systems of record from analytical limitations.",
        },
        {
            "id": "system_map",
            "type": "markdown",
            "body": markdown_records(
                results["system_map"],
                "stage",
                [("system_of_record", "System of record"),
                 ("primary_writes", "Material writes"), ("automation", "Mechanism"),
                 ("trust_risk", "Trust risk")],
            ),
        },
        {
            "id": "trust_heading",
            "type": "markdown",
            "body": "## Reliable analysis requires explicit eligibility windows\n\nBooked demand is broadly usable after date and status normalization. Execution timing, schedule churn, staff activity, payments, and flight behavior require narrower rules. Historical GPS paths, decision-time flight forecasts, and populated capacity/demand aggregates are missing rather than merely incomplete.",
        },
        {
            "id": "trust_matrix",
            "type": "markdown",
            "body": markdown_records(
                results["trust_matrix"],
                "data_asset",
                [("coverage", "Coverage"), ("trust", "Trust"), ("safe_use", "Safe use"),
                 ("limitation", "Limitation")],
            ),
        },
        {
            "id": "quality_heading",
            "type": "markdown",
            "body": "## Workflow differences create the most important data defects\n\nDriver and dispatcher status paths create event history, while bulk admin completion updates current state directly. Repeated events then make first-versus-last timestamp selection consequential. These are product workflow issues with analytical consequences—not isolated cleanup tasks.",
        },
        {
            "id": "quality_findings",
            "type": "markdown",
            "body": markdown_records(
                findings,
                "finding",
                [("severity", "Severity"), ("evidence", "Evidence"),
                 ("analytical_risk", "Why it matters"),
                 ("recommended_fix", "Recommended fix")],
            ),
        },
        {
            "id": "coverage_interpretation",
            "type": "markdown",
            "body": f"## Status history becomes usable only recently\n\nThe line chart begins at the algorithmically selected instrumentation boundary of **{status['instrumentation_start']}**: the first month with at least 100 completed legs and at least 75% timely completion-event capture. Differences between first-event and last-event validity show why transition semantics must be fixed before an on-time KPI is institutionalized.",
        },
        {"id": "chain_coverage", "type": "chart", "chartId": "chain_coverage_chart", "layout": "full"},
        {
            "id": "timing_interpretation",
            "type": "markdown",
            "body": "## Route history is promising but not yet an operational control\n\nThe scheduler reads a derived route bucket only after five samples for display estimates, while chain feasibility deliberately uses static Orlando-area travel times, a 45-minute airport dwell assumption, and small buffers. Thin and stale buckets make a governed shadow comparison the safe next step.",
        },
        {"id": "route_confidence", "type": "chart", "chartId": "route_confidence_chart", "layout": "full"},
        {"id": "route_baseline_text", "type": "markdown", "body": f"### Chronological drive-duration baseline\n\n{route_comparison}"},
        {"id": "route_segments", "type": "table", "tableId": "route_table", "layout": "full"},
        {
            "id": "demand_heading",
            "type": "markdown",
            "body": f"## Demand forecasting can begin with a transparent baseline\n\n{demand_comparison} The volume chart shows booked service dates, not verified completed trips, and the current month may be partial.",
        },
        {"id": "monthly_volume", "type": "chart", "chartId": "monthly_volume_chart", "layout": "full"},
        {
            "id": "kpi_heading",
            "type": "markdown",
            "body": "## Measure service reliability, coverage, and schedule stability\n\nThe KPI system should stay small: three outcomes supported by operational drivers and safety guardrails. Targets should wait until at least four to eight weeks of stable, source-aware instrumentation establish a defensible baseline.",
        },
        {
            "id": "kpi_framework",
            "type": "markdown",
            "body": markdown_records(
                results["kpi_framework"],
                "metric",
                [("role", "Role"), ("definition", "Definition"), ("cadence", "Cadence"),
                 ("current_readiness", "Readiness"),
                 ("required_work", "Required work")],
            ),
        },
        {
            "id": "predictive_heading",
            "type": "markdown",
            "body": "## Predictive work should remain in shadow mode\n\nSimple duration and demand baselines are feasible for learning and measurement. Delay and farm-out automation are blocked by missing decision-time observations or ambiguous labels. A model should never be trained on the latest overwritten state and presented as though it knew that information at dispatch time.",
        },
        {
            "id": "predictive_readiness",
            "type": "markdown",
            "body": markdown_records(
                results["predictive_matrix"],
                "candidate",
                [("label_quality", "Label quality"), ("prototype_result", "Prototype"),
                 ("decision", "Decision"),
                 ("next_gate", "Next gate")],
            ),
        },
        {
            "id": "roadmap_heading",
            "type": "markdown",
            "body": "## The roadmap starts with event integrity, not machine learning\n\nPhase 1 makes operational decisions observable and testable. Phase 2 turns that foundation into governed scorecards. Phase 3 introduces shadow decision support. Phase 4 permits only bounded automation with monitoring, rollback, and owner approval.",
        },
        {
            "id": "roadmap_priority",
            "type": "markdown",
            "body": (
                "### Impact–effort matrix\n\n"
                "- **High impact / medium effort:** canonical transitions, immutable events, flight observations, and schedule decision snapshots.\n"
                "- **High impact / low effort:** nightly data-quality gates.\n"
                "- **Medium-to-high impact / medium effort:** governed refreshes, weekly scorecards, reconciliation, and shadow baselines.\n"
                "- **Conditional impact / high effort:** dispatcher decision support and production predictive automation."
            ),
        },
        {
            "id": "roadmap_detail",
            "type": "markdown",
            "body": markdown_records(
                results["roadmap"],
                "recommendation",
                [("phase", "Phase"), ("impact", "Impact"), ("effort", "Effort"),
                 ("priority_score", "Priority score"), ("confidence", "Confidence"),
                 ("why", "Why it matters"), ("problem", "Problem addressed"),
                 ("expected_impact", "Expected impact"), ("complexity", "Complexity"),
                 ("dependencies", "Dependencies"),
                 ("risks_and_guardrails", "Risks and guardrails")],
            ),
        },
        {
            "id": "questions_heading",
            "type": "markdown",
            "body": "## Further questions for operations\n\nThe requested targeted validation pass could not be completed without an operations stakeholder. These eight questions are deliberately limited to decisions that change metric definitions, model labels, or roadmap confidence; unanswered items remain caveats.",
        },
        {
            "id": "validation_questions",
            "type": "markdown",
            "body": markdown_records(
                results["validation_questions"],
                "question",
                [("priority", "Priority"), ("decision_affected", "Decision affected"),
                 ("status", "Status")],
            ),
        },
        {
            "id": "caveats",
            "type": "markdown",
            "body": (
                "## Caveats and Assumptions\n\n"
                f"- **Validation status: {validation_rating}.** Source integrity passed SQLite quick-check (`{profile['quick_check']}`), but production freshness is unverified because `{profile['requested_path']}` was absent.\n"
                f"- The analysis cutoff is **{profile['analysis_cutoff']}** in **America/New_York**. Django timestamps are treated as UTC and converted before comparison with local service dates.\n"
                "- Status-chain and duration findings use bounded cohorts, first-event semantics, in-house drivers, and explicit plausibility limits. They are associations and operational proxies, not causal claims.\n"
                "- Route categories are privacy-preserving operational groupings. Historical GPS, complete driver-contact timing, immutable schedule decisions, and flight forecast observations were unavailable.\n"
                "- No production database, application behavior, schema, schedule, or external system was changed."
            ),
        },
    ]

    access_issues = []
    if not profile["fresh_snapshot_present"]:
        access_issues.append(
            {
                "id": "fresh_snapshot_missing",
                "dataset": "all",
                "message": f"Required production snapshot {profile['requested_path']} was unavailable; values come from {profile['selected_path']} and are preliminary.",
            }
        )
    access_issues.append(
        {
            "id": "operations_validation_pending",
            "dataset": "validation_questions",
            "message": "Targeted operations stakeholder validation is pending; unresolved semantics reduce recommendation confidence.",
        }
    )

    return {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "Grayson Towncar Operations Intelligence",
            "description": "Evidence-backed audit of operational data trust, scheduling assumptions, analytics opportunities, and roadmap priorities.",
            "generatedAt": generated_at,
            "cards": cards,
            "charts": charts,
            "tables": tables,
            "sources": manifest_sources,
            "blocks": blocks,
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": snapshot_status,
            "datasets": {
                "summary": summary,
                "source_profile": [profile],
                "table_inventory": results["table_inventory"],
                "status_summary": [{
                    "instrumentation_start": status["instrumentation_start"],
                    "cohort_completed_legs": status["cohort_completed_legs"],
                    "cohort_chain_rate": status["cohort_chain_rate"],
                    "cohort_timely_completion_rate": status["cohort_timely_completion_rate"],
                    "semantic_disagreement": status["semantic_disagreement"],
                    "legs_with_repeated_status": status["legs_with_repeated_status"],
                    "late_backfill_completed_events": status["late_backfill_completed_events"],
                    "exact_duplicate_events": status["exact_duplicate_events"],
                }],
                "status_repeat_summary": status["repeat_summary"],
                "system_map": results["system_map"],
                "trust_matrix": results["trust_matrix"],
                "quality_findings": findings,
                "chain_trend": chain_trend,
                "route_metric_summary": [{
                    "total_buckets": route_metrics["total_buckets"],
                    "reliable_buckets": route_metrics["reliable_buckets"],
                    "high_confidence_buckets": route_metrics["high_confidence_buckets"],
                    "stale_buckets_14d": route_metrics["stale_buckets_14d"],
                }],
                "route_confidence": route_metrics["confidence"],
                "route_summary": route_baseline["route_summary"],
                "route_baseline_summary": [{
                    "eligible_observations": route_baseline["eligible_observations"],
                    "train_observations": route_baseline["train_observations"],
                    "test_observations": route_baseline["test_observations"],
                    "static_mae_minutes": route_baseline["static_mae_minutes"],
                    "historical_median_mae_minutes": route_baseline["historical_median_mae_minutes"],
                    "mae_improvement": route_baseline["mae_improvement"],
                    "dwell_samples": route_baseline["dwell_samples"],
                    "dwell_median_minutes": route_baseline["dwell_median_minutes"],
                    "dwell_p75_minutes": route_baseline["dwell_p75_minutes"],
                    "dwell_p90_minutes": route_baseline["dwell_p90_minutes"],
                }],
                "monthly_volume": demand["monthly_volume"],
                "demand_baseline_summary": [{
                    "daily_rows": demand["daily_rows"],
                    "train_days": demand["train_days"],
                    "test_days": demand["test_days"],
                    "weekday_median_mae_legs": demand["weekday_median_mae_legs"],
                    "overall_median_mae_legs": demand["overall_median_mae_legs"],
                }],
                "operations_summary": [{
                    **operations["leg_summary"],
                    "assignment_lead_samples": operations["assignment_lead_samples"],
                    "assignment_lead_median_hours": operations["assignment_lead_median_hours"],
                    "assignment_lead_p25_hours": operations["assignment_lead_p25_hours"],
                    "snapshot_comparable_assignments": operations["snapshot_comparable_assignments"],
                    "snapshot_assignment_changes": operations["snapshot_assignment_changes"],
                    "snapshot_change_rate": operations["snapshot_change_rate"],
                }],
                "payment_summary": [operations["payment_summary"]],
                "flight_summary": [results["flights"]],
                "kpi_framework": results["kpi_framework"],
                "predictive_matrix": results["predictive_matrix"],
                "roadmap": results["roadmap"],
                "validation_questions": results["validation_questions"],
            },
            "accessIssues": access_issues,
        },
        "sources": sources,
        "package_info": {
            "originUrl": "artifact://grayson-towncar-operations-intelligence",
            "controls": {"edit": False, "refresh": False},
        },
    }


def run_analysis(
    database_path: str | Path = EXPECTED_SNAPSHOT,
    cutoff: date = DEFAULT_CUTOFF,
    allow_preliminary: bool = True,
) -> dict[str, Any]:
    context = choose_database(database_path, allow_preliminary=allow_preliminary)
    database = ReadOnlySQLite(Path(context.selected_path))
    try:
        profile = source_profile(database, context, cutoff)
        inventory = build_table_inventory(database)
        status = analyze_status_history(database, cutoff)
        dates = analyze_dates_and_categories(database, cutoff)
        route_metrics = analyze_route_metrics(database, cutoff)
        route_baseline = analyze_route_duration_baseline(database, status, cutoff)
        demand = analyze_demand_baseline(database, status["instrumentation_start"], cutoff)
        operations = analyze_operations(database, status["instrumentation_start"], cutoff)
        flights = analyze_flights(database, status["instrumentation_start"], cutoff)
        system_map = build_system_map()
        trust_matrix = build_trust_matrix(inventory, status, route_metrics, operations, flights)
        quality_findings = build_quality_findings(
            profile, dates, status, route_metrics, inventory, operations
        )
        kpis = build_kpi_framework()
        predictive = build_predictive_matrix(route_baseline, demand)
        roadmap = build_roadmap(database)
        questions = build_validation_questions()
        generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        results = {
            "generated_at": generated_at,
            "source_profile": profile,
            "table_inventory": inventory,
            "dates_and_categories": dates,
            "status_history": status,
            "route_metrics": route_metrics,
            "route_baseline": route_baseline,
            "demand_baseline": demand,
            "operations": operations,
            "flights": flights,
            "system_map": system_map,
            "trust_matrix": trust_matrix,
            "quality_findings": quality_findings,
            "kpi_framework": kpis,
            "predictive_matrix": predictive,
            "roadmap": roadmap,
            "validation_questions": questions,
            "chart_map": build_chart_map(),
        }
        results["artifact"] = build_artifact(results, generated_at)
        return results
    finally:
        database.close()


def json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    return value


def write_outputs(results: dict[str, Any], artifact_path: Path, notes_path: Path) -> None:
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(json_safe(results["artifact"]), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    notes = {key: value for key, value in results.items() if key != "artifact"}
    notes_path.write_text(
        json.dumps(json_safe(notes), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=EXPECTED_SNAPSHOT.as_posix())
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF.isoformat())
    parser.add_argument(
        "--artifact",
        default="docs/operations-intelligence/artifact.json",
    )
    parser.add_argument(
        "--notes",
        default="scratch/operations_intelligence/analysis_results.json",
    )
    parser.add_argument(
        "--no-preliminary-fallback",
        action="store_true",
        help="Fail instead of using content/db.sqlite3 when the requested snapshot is absent.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    results = run_analysis(
        database_path=args.db,
        cutoff=date.fromisoformat(args.cutoff),
        allow_preliminary=not args.no_preliminary_fallback,
    )
    write_outputs(
        results,
        REPOSITORY_ROOT / args.artifact,
        REPOSITORY_ROOT / args.notes,
    )
    profile = results["source_profile"]
    print(
        json.dumps(
            {
                "artifact": args.artifact,
                "notes": args.notes,
                "source": profile["selected_path"],
                "source_tier": profile["source_tier"],
                "quick_check": profile["quick_check"],
                "snapshot_status": results["artifact"]["snapshot"]["status"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
