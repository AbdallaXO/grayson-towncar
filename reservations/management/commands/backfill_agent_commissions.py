from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum
from django.db.models.functions import Coalesce

from reservations.models import Reservation
from users.models import TravelAgent


class Command(BaseCommand):
    help = "Backfill NULL commission_amount on completed reservations and recalculate agent unpaid_commissions"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be updated without making changes",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        # Step 1: Backfill NULL commission_amount on reservations that have a travel agent
        null_commissions = Reservation.objects.filter(
            travel_agent__isnull=False,
            commission_amount__isnull=True,
            base_price__isnull=False,
        )

        backfill_count = 0
        for res in null_commissions:
            agent = res.travel_agent
            rate = (agent.commission_rate or Decimal("10")) / Decimal("100")
            amount = res.base_price * rate

            if dry_run:
                self.stdout.write(
                    f"  Would set Reservation #{res.id} commission_amount = ${amount:.2f} "
                    f"(base_price=${res.base_price}, rate={agent.commission_rate}%)"
                )
            else:
                res.commission_amount = amount
                res.save(update_fields=["commission_amount"])

            backfill_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"{'Would backfill' if dry_run else 'Backfilled'} {backfill_count} reservations with NULL commission_amount"
            )
        )

        # Step 2: Recalculate unpaid_commissions for all agents
        agents = TravelAgent.objects.all()
        update_count = 0

        for agent in agents:
            unpaid_total = (
                Reservation.objects.filter(
                    travel_agent=agent,
                    commission_paid=False,
                    status="completed",
                ).aggregate(
                    total=Coalesce(Sum("commission_amount"), Decimal("0"))
                )["total"]
            )

            if agent.unpaid_commissions != unpaid_total:
                if dry_run:
                    self.stdout.write(
                        f"  Would update {agent.agent_name}: "
                        f"${agent.unpaid_commissions} -> ${unpaid_total}"
                    )
                else:
                    agent.unpaid_commissions = unpaid_total
                    agent.save(update_fields=["unpaid_commissions"])

                update_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"{'Would update' if dry_run else 'Updated'} {update_count} agents' unpaid_commissions"
            )
        )
