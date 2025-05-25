from django.core.management.base import BaseCommand
from users.models import TravelAgent


class Command(BaseCommand):
    help = "Fix agent total paid commissions by syncing with actual payouts"

    def add_arguments(self, parser):
        parser.add_argument(
            "--agent", type=str, help="Username or email of specific agent to fix"
        )
        parser.add_argument("--all", action="store_true", help="Fix all agents")

    def handle(self, *args, **options):
        if options["agent"]:
            try:
                agent = TravelAgent.objects.get(user__username=options["agent"])
                self.fix_agent(agent)
            except TravelAgent.DoesNotExist:
                try:
                    agent = TravelAgent.objects.get(user__email=options["agent"])
                    self.fix_agent(agent)
                except TravelAgent.DoesNotExist:
                    self.stdout.write(
                        self.style.ERROR(f"Agent not found: {options['agent']}")
                    )
        elif options["all"]:
            agents = TravelAgent.objects.all()
            for agent in agents:
                self.fix_agent(agent)
        else:
            self.stdout.write(
                self.style.ERROR("Please specify either --agent or --all")
            )

    def fix_agent(self, agent):
        old_total = agent.total_paid_commission
        new_total = agent.sync_total_paid_commission()
        self.stdout.write(
            self.style.SUCCESS(f"Fixed agent {agent}: {old_total} -> {new_total}")
        )
