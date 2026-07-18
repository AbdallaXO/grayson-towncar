"""Staff-only live Postgres connection probe.

Added for the connection-saturation incident (2026-07-18) to verify the leak is
fixed. Success looks like: `total` PLATEAUS at a stable low number over time
instead of climbing toward `max_connections`. A background-thread leak shows up
as a growing count of rows with state='idle'.

    GET /dispatching/db-connections/   (staff only) -> JSON
"""
from django.contrib.admin.views.decorators import staff_member_required
from django.db import connection
from django.http import JsonResponse


@staff_member_required
def db_connection_stats(request):
    if connection.vendor != "postgresql":
        return JsonResponse({"vendor": connection.vendor, "detail": "not postgresql (dev)"})

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) AS n, state, application_name
            FROM pg_stat_activity
            WHERE datname = current_database()
            GROUP BY state, application_name
            ORDER BY n DESC
            """
        )
        by_state_and_app = [
            {"count": n, "state": state, "application_name": app or ""}
            for (n, state, app) in cur.fetchall()
        ]
        cur.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        )
        total = cur.fetchone()[0]
        cur.execute("SHOW max_connections")
        max_connections = int(cur.fetchone()[0])

    idle = sum(r["count"] for r in by_state_and_app if r["state"] == "idle")
    active = sum(r["count"] for r in by_state_and_app if r["state"] == "active")

    return JsonResponse(
        {
            "total": total,
            "max_connections": max_connections,
            "idle": idle,
            "active": active,
            "by_state_and_app": by_state_and_app,
        }
    )
