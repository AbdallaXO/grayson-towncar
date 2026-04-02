from decimal import Decimal
from django.core.management.base import BaseCommand
from reservations.models import Reservation
from users.models import TravelAgent


class Command(BaseCommand):
    help = "Backfill commission_amount on reservations and update agent unpaid_commissions"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be updated without making changes",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        # Step 1: Backfill commission_amount on reservations that have a travel agent but NULL commission
        null_commission_qs = Reservation.objects.filter(
            travel_agent__isnull=False,
            commission_amount__isnull=True,
            base_price__isnull=False,
        ).select_related("travel_agent")

        updated_res = 0
        for res in null_commission_qs:
            rate = res.travel_agent.commission_rate / Decimal("100") if res.travel_agent.commission_rate else Decimal("0.10")
            amount = res.base_price * rate
            if dry_run:
                self.stdout.write(f"  Would set Reservation #{res.id} commission_amount = ${amount:.2f}")
            else:
                res.commission_amount = amount
                res.save(update_fields=["commission_amount"])
            updated_res += 1

        self.stdout.write(self.style.SUCCESS(
            f"{'Would update' if dry_run else 'Updated'} {updated_res} reservations with NULL commission_amount"
        ))

        # Step 2: Recalculate unpaid_commissions for all active agents
        agents = TravelAgent.objects.filter(is_active=True)
        updated_agents = 0
        for agent in agents:
            old_unpaid = agent.unpaid_commissions
            new_unpaid = agent.calculate_unpaid_commissions()
            if old_unpaid != new_unpaid:
                if dry_run:
                    self.stdout.write(
                        f"  Would update {agent.agent_name or agent.user.username}: "
                        f"${old_unpaid} -> ${new_unpaid}"
                    )
                else:
                    agent.unpaid_commissions = new_unpaid
                    agent.save(update_fields=["unpaid_commissions"])
                    self.stdout.write(
                        f"  Updated {agent.agent_name or agent.user.username}: "
                        f"${old_unpaid} -> ${new_unpaid}"
                    )
                updated_agents += 1

        self.stdout.write(self.style.SUCCESS(
            f"{'Would update' if dry_run else 'Updated'} {updated_agents} agents with stale unpaid_commissions"
        ))
