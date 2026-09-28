"""
Merge travel agents who hold two accounts one capital apart.

Agent registration used to check usernames case-SENSITIVELY, so "JamieTodd"
could register again as "jamietodd". The agent sign-in then looked the name up
with ``.get(username__iexact=...)`` and raised a 500 for both accounts. The
sign-in is fixed (users/views.py ``_authenticate_identifier``) and a unique
index on LOWER(username) now stops new pairs; this command folds the existing
pairs into one account each.

For every pair (keep, retire):
  * every row pointing at the retired TravelAgent — reservations, commission
    payouts, anything else with a FK to it — is repointed at the kept one;
  * blank profile fields on the kept agent are filled from the retired one;
  * the retired login is switched OFF and renamed "<old>.merged-<keep id>", so
    it can never collide again, and its TravelAgent is marked inactive. Nothing
    is deleted: the rename is the audit trail and the way back;
  * both agents' commission totals are recomputed from the rows themselves
    (the same methods the admin's fix command uses), never added by hand.

Writes go through QuerySet.update(), so no post_save signal fires — no welcome
email, no booking email. Dry run by default: the whole merge runs inside a
transaction and is rolled back, so the report shows exactly what --apply will
do. Every pair is verified by id AND expected username before anything moves;
one mismatch aborts the lot.

    python manage.py merge_duplicate_agents            # dry run
    python manage.py merge_duplicate_agents --apply
"""
import json
from datetime import datetime

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from users.models import TravelAgent

# (keep user id, keep username, retire user id, retire username, why keep)
# Chosen from the production data on 2026-09-28: keep the account holding the
# work (bookings, payouts, correct email); retire the empty or mistyped one.
PAIRS = [
    (974, "gennyslack@mousecounselors.com", 940, "Gennyslack@mousecounselors.com",
     "31 bookings, payouts and Venmo on the kept one; the other is empty (email saved as 'ge')"),
    (348, "hannahwarren", 126, "HannahWarren",
     "kept one has the correct email (.com, not .om); 1 booking + 1 payout move over"),
    (1314, "jamietodd", 124, "JamieTodd",
     "kept one holds her booking; the other is empty"),
    (1439, "LindsaySchroeder", 1438, "lindsayschroeder",
     "kept one is the agent account; the other was never an agent"),
    (789, "Sunshinetravels17@yahoo.com", 294, "sunshinetravels17@yahoo.com",
     "both empty; kept the one used most recently"),
]

FILL_BLANKS = ("agent_name", "agency_name", "phone", "payment_method", "payment_info")


class _DryRun(Exception):
    pass


class Command(BaseCommand):
    help = "Fold travel agents' case-duplicate accounts into one (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the merge.")
        parser.add_argument("--log", default="", help="Write a JSON record of every change here.")

    def handle(self, *args, **options):
        apply = options["apply"]
        record = {"at": datetime.now().isoformat(timespec="seconds"), "applied": apply, "pairs": []}
        try:
            with transaction.atomic():
                for pair in PAIRS:
                    record["pairs"].append(self._merge(*pair))
                if not apply:
                    raise _DryRun
        except _DryRun:
            self.stdout.write(self.style.WARNING("\nDRY RUN - rolled back. Re-run with --apply to write it."))
        else:
            self.stdout.write(self.style.SUCCESS("\nApplied."))
        if options["log"]:
            with open(options["log"], "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2, default=str)
            self.stdout.write(f"Record written to {options['log']}")

    # ------------------------------------------------------------------
    def _merge(self, keep_id, keep_name, retire_id, retire_name, why):
        keep = User.objects.filter(pk=keep_id).first()
        retire = User.objects.filter(pk=retire_id).first()
        if keep is None or retire is None:
            raise CommandError(f"Pair {keep_id}/{retire_id}: an account is missing — nothing written.")
        if keep.username != keep_name or retire.username != retire_name:
            # Already merged (renamed) is the one expected mismatch: say so, skip.
            if retire.username.startswith(f"{retire_name}.merged-"):
                self.stdout.write(f"\n{retire_name}: already merged into {keep.username} - skipped.")
                return {"keep": keep_id, "retire": retire_id, "skipped": "already merged"}
            raise CommandError(
                f"Pair {keep_id}/{retire_id} is not what was reviewed "
                f"({keep.username!r} / {retire.username!r}) — nothing written.")

        keep_agent = TravelAgent.objects.filter(user=keep).first()
        retire_agent = TravelAgent.objects.filter(user=retire).first()
        if keep_agent is None:
            raise CommandError(f"{keep_name} has no travel-agent profile — nothing written.")

        self.stdout.write(f"\n{retire_name}  ->  {keep_name}   ({why})")
        entry = {"keep": keep_id, "keep_username": keep_name, "retire": retire_id,
                 "retire_username": retire_name, "moved": {}, "filled": {}}

        if retire_agent is not None:
            for rel in TravelAgent._meta.related_objects:
                if rel.many_to_many or rel.one_to_one:
                    accessor = rel.get_accessor_name()
                    if rel.one_to_one and hasattr(retire_agent, accessor):
                        raise CommandError(f"{retire_name}: one-to-one {accessor} exists — merge by hand.")
                    if rel.many_to_many and getattr(retire_agent, accessor).exists():
                        raise CommandError(f"{retire_name}: many-to-many {accessor} rows — merge by hand.")
                    continue
                model, field = rel.related_model, rel.field.name
                qs = model._base_manager.filter(**{field: retire_agent})
                ids = list(qs.values_list("pk", flat=True))
                if ids:
                    qs.update(**{field: keep_agent})
                    label = f"{model._meta.label}.{field}"
                    entry["moved"][label] = ids
                    self.stdout.write(f"  moved {len(ids):>3} x {label}")

            fills = {f: getattr(retire_agent, f) for f in FILL_BLANKS
                     if not getattr(keep_agent, f) and getattr(retire_agent, f)}
            if fills:
                TravelAgent.objects.filter(pk=keep_agent.pk).update(**fills)
                entry["filled"] = fills
                self.stdout.write(f"  filled blanks on the kept agent: {', '.join(fills)}")
            TravelAgent.objects.filter(pk=retire_agent.pk).update(is_active=False)

        new_name = f"{retire_name}.merged-{keep_id}"[:150]
        User.objects.filter(pk=retire.pk).update(username=new_name, is_active=False)
        entry["retired_as"] = new_name
        self.stdout.write(f"  retired login renamed to {new_name!r} and switched off")

        # Totals from the rows, never by hand — and only where rows moved. A
        # merge that moved nothing must not quietly restate anyone's money.
        touched = [keep_agent, retire_agent] if entry["moved"] else []
        for agent in touched:
            agent.refresh_from_db()
            paid = agent.sync_total_paid_commission()
            stats = agent.update_commission_stats()
            self.stdout.write(f"  {agent.user.username}: paid {paid} / unpaid {stats['unpaid']}"
                              f" / pending {stats['pending']}")
        keep_agent.refresh_from_db()
        entry["kept_totals"] = {"paid": keep_agent.total_paid_commission,
                                "unpaid": keep_agent.unpaid_commissions,
                                "pending": keep_agent.pending_commissions}
        return entry
