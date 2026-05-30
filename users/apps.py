import os

from django.apps import AppConfig


class UsersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "users"

    def ready(self):
        from . import signals

        # In read-only prod mode, logging in would try to write last_login to
        # the read-only DB and 500. Drop that write so the UI stays usable.
        if os.environ.get("USE_PROD_RO") == "1":
            from django.contrib.auth.signals import user_logged_in

            # Django connects update_last_login with dispatch_uid="update_last_login";
            # it must be disconnected by that same uid, not by the function ref.
            user_logged_in.disconnect(dispatch_uid="update_last_login")
