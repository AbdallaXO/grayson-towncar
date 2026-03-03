"""
Locust load-test script for Grayson Towncar dispatcher endpoints.

Usage (from project root, with an active venv that has locust installed):
    pip install locust
    locust -f locustfile.py --host=https://graysontowncar.com

You'll need a valid session cookie. Either:
  a) Use `on_start` to POST to /accounts/login/ with credentials (set env vars below), or
  b) Copy a session cookie from your browser and set DJANGO_SESSION_COOKIE below.

Environment variables:
  LOCUST_USERNAME   — Django admin/dispatcher username
  LOCUST_PASSWORD   — Django admin/dispatcher password
  LOCUST_DATE       — YYYY-MM-DD date to use for legs/capacity queries (defaults to today)
"""

import os
from datetime import date
from locust import HttpUser, task, between


USERNAME = os.environ.get("LOCUST_USERNAME", "")
PASSWORD = os.environ.get("LOCUST_PASSWORD", "")
TARGET_DATE = os.environ.get("LOCUST_DATE", date.today().isoformat())


class DispatcherUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        """Log in once per simulated user before running tasks."""
        # Fetch CSRF token from login page
        resp = self.client.get("/accounts/login/")
        csrf = resp.cookies.get("csrftoken", "")
        self.client.post(
            "/accounts/login/",
            data={
                "username": USERNAME,
                "password": PASSWORD,
                "csrfmiddlewaretoken": csrf,
            },
            headers={"Referer": self.host + "/accounts/login/"},
            name="/accounts/login/",
        )

    @task(3)
    def dispatcher_dashboard(self):
        self.client.get("/dispatching/", name="GET /dispatching/")

    @task(5)
    def legs_list(self):
        self.client.get(
            f"/dispatching/legs/?date={TARGET_DATE}",
            name="GET /dispatching/legs/",
        )

    @task(2)
    def capacity_planner(self):
        self.client.get(
            f"/dispatching/capacity-planner/?date={TARGET_DATE}",
            name="GET /dispatching/capacity-planner/",
        )

    @task(2)
    def confirmations(self):
        self.client.get(
            f"/dispatching/confirmations/?date={TARGET_DATE}",
            name="GET /dispatching/confirmations/",
        )
