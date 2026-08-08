"""
Delete spam contact form submissions and the dispatcher tasks they spawned.

Scoring lives in users/spam.py — the same rules that now reject spam at
submission time, so this command and the live form can never disagree about
what counts as spam.

Usage:
    python manage.py clean_spam_contacts --dry-run   # always look first
    python manage.py clean_spam_contacts
"""

from django.core.management.base import BaseCommand

from users.models import ContactUsForm
from users.spam import BLOCK_THRESHOLD, score_instance


class Command(BaseCommand):
    help = "Delete spam contact form submissions (and their open ops tasks)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Show what would be deleted without actually deleting",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        scored = []
        for entry in ContactUsForm.objects.all():
            score, reasons = score_instance(entry)
            if score >= BLOCK_THRESHOLD:
                scored.append((entry, score, reasons))

        if not scored:
            self.stdout.write(self.style.SUCCESS("No spam found."))
            return

        scored.sort(key=lambda row: -row[1])
        for entry, score, reasons in scored:
            name = f"{entry.first_name} {entry.last_name}"[:50]
            self.stdout.write(
                f"  #{entry.id} [score {score}] {name} - {entry.email} - {entry.created_at}"
            )
            self.stdout.write(f"      {', '.join(reasons)}")

        ids = [entry.id for entry, _score, _reasons in scored]

        # Tasks FK to the form with on_delete=CASCADE, so deleting the spam
        # clears the dispatcher's queue in the same step. Count them up front
        # so the dry run reports the real blast radius.
        task_count = 0
        try:
            from ops.models import OperationalTask

            task_count = OperationalTask.objects.filter(contact_form_id__in=ids).count()
        except Exception as exc:  # ops app unavailable — deletion still valid
            self.stdout.write(self.style.WARNING(f"Could not count ops tasks: {exc}"))

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"\nDry run: {len(scored)} spam entries found (not deleted), "
                f"{task_count} linked ops tasks would go with them."
            ))
            return

        ContactUsForm.objects.filter(id__in=ids).delete()
        self.stdout.write(self.style.SUCCESS(
            f"\nDeleted {len(scored)} spam entries and {task_count} linked ops tasks."
        ))
