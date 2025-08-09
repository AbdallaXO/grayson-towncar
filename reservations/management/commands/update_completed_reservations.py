from django.core.management.base import BaseCommand
from reservations.models import Reservation


class Command(BaseCommand):
    help = 'Update reservations to completed status if all their legs are completed'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be updated without making changes',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        
        # Get reservations that are not already completed or cancelled
        reservations = Reservation.objects.exclude(status__in=['completed', 'cancelled'])
        
        updated_count = 0
        
        for reservation in reservations:
            # Check if this reservation should be completed
            legs = reservation.legs.all()
            
            if legs.exists() and all(leg.status == 'completed' for leg in legs):
                if dry_run:
                    self.stdout.write(
                        self.style.SUCCESS(f'Would update Reservation #{reservation.id} to completed')
                    )
                else:
                    reservation.status = 'completed'
                    reservation.save(update_fields=['status'])
                    self.stdout.write(
                        self.style.SUCCESS(f'Updated Reservation #{reservation.id} to completed')
                    )
                updated_count += 1
        
        if dry_run:
            self.stdout.write(
                self.style.WARNING(f'Dry run complete. Would update {updated_count} reservations.')
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f'Successfully updated {updated_count} reservations to completed status.')
            )
