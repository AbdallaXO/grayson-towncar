"""
Task runner that executes tasks in background threads.

Replaces Celery's .delay() — all tasks run in daemon threads
within the Django/Gunicorn process. No external broker needed.
"""

import logging
import threading

logger = logging.getLogger(__name__)


def run_in_background(func, *args, **kwargs):
    """
    Run a function in a background daemon thread.
    Replaces task.delay() for all follow-up engine tasks.
    """
    def wrapper():
        try:
            func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Background task {func.__name__} failed: {e}", exc_info=True)
        finally:
            # Release this thread's DB connection. Django opens one connection per
            # thread and CONN_MAX_AGE keeps it alive for minutes; a burst of tasks
            # would otherwise pile up idle connections and exhaust Postgres
            # (max_connections=100) — the connection-saturation outage 2026-07-18.
            # Mirrors reservations.utils._run_in_background.
            from django.db import connections
            connections.close_all()

    thread = threading.Thread(target=wrapper, daemon=True, name=f"task-{func.__name__}")
    thread.start()
    return thread
