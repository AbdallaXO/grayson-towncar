"""
Django management command to populate airline_display_name for existing Flight records.

This command will update all Flight objects in the database to have their
airline_display_name field populated based on their existing airline IATA codes.

This is the "reverse" of normalize_airlines - instead of normalizing names to codes,
this populates display names from codes.

Optimized for large databases with batch processing and bulk updates.

Usage:
    python manage.py populate_airline_display_names
    
    # Dry run (show what would be changed without saving):
    python manage.py populate_airline_display_names --dry-run
    
    # Verbose output (show sample changes):
    python manage.py populate_airline_display_names --verbose
    
    # Custom batch size (default 500):
    python manage.py populate_airline_display_names --batch-size 1000
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from reservations.models import Flight
from reservations.utils import get_airline_display_name


class Command(BaseCommand):
    help = 'Populate airline_display_name for all existing Flight records based on their airline IATA codes'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be changed without actually saving',
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show sample changes (first 20)',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=500,
            help='Number of flights to process in each batch (default: 500)',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Update all flights, even if they already have a display_name',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        verbose = options['verbose']
        batch_size = options['batch_size']
        force = options['force']
        
        # Get all flights with airline data
        if force:
            # Update all flights with airline codes
            flights_queryset = Flight.objects.filter(
                Q(airline__isnull=False) & ~Q(airline='')
            ).only('id', 'airline', 'airline_display_name')
        else:
            # Only update flights that don't have display_name or have empty display_name
            flights_queryset = Flight.objects.filter(
                Q(airline__isnull=False) & ~Q(airline='') &
                (Q(airline_display_name__isnull=True) | Q(airline_display_name=''))
            ).only('id', 'airline', 'airline_display_name')
        
        total_flights = flights_queryset.count()
        
        # Edge case: No flights with airline data
        if total_flights == 0:
            self.stdout.write(
                self.style.WARNING('\nNo flights found that need airline_display_name populated.\n')
            )
            if not force:
                self.stdout.write('This could mean:')
                self.stdout.write('  - No flights exist in the database')
                self.stdout.write('  - All flights already have airline_display_name set')
                self.stdout.write('  - All flights have empty/null airline fields')
                self.stdout.write('\nUse --force to update all flights regardless of existing display_name.')
            return
        
        updated_count = 0
        unchanged_count = 0
        changes = []
        flights_to_update = []
        
        self.stdout.write(
            self.style.SUCCESS(f'\nFound {total_flights:,} flights to check.\n')
        )
        
        if dry_run:
            self.stdout.write(
                self.style.WARNING('DRY RUN MODE - No changes will be saved.\n')
            )
        
        self.stdout.write(f'Processing in batches of {batch_size:,}...\n')
        
        # Process flights in batches to avoid memory issues
        processed = 0
        for flight in flights_queryset.iterator(chunk_size=batch_size):
            original_airline = flight.airline or ""
            original_display_name = flight.airline_display_name or ""
            needs_update = False
            
            # Get display name from airline code
            if original_airline:
                new_display_name = get_airline_display_name(original_airline)
                
                # Only update if display name is different or empty
                if new_display_name and (force or not original_display_name or original_display_name != new_display_name):
                    # If we got a display name (not just the code), update it
                    if new_display_name != original_airline:
                        flight.airline_display_name = new_display_name
                        needs_update = True
                    elif force and not original_display_name:
                        # If get_airline_display_name returned the code (unknown airline),
                        # we can still set it if forcing and it's empty
                        flight.airline_display_name = new_display_name
                        needs_update = True
            
            if needs_update:
                updated_count += 1
                flights_to_update.append(flight)
                
                # Store sample changes for display
                if len(changes) < 20:
                    change_desc = f"airline_display_name: '{original_display_name or '(empty)'}' -> '{flight.airline_display_name}'"
                    changes.append({
                        'id': flight.id,
                        'airline': original_airline,
                        'changes': change_desc,
                    })
                
                if verbose and len(changes) <= 20:
                    self.stdout.write(
                        f"Flight #{flight.id} ({original_airline}): "
                        f"'{original_display_name or '(empty)'}' -> '{flight.airline_display_name}'"
                    )
            else:
                unchanged_count += 1
            
            processed += 1
            
            # Progress indicator every 100 flights
            if processed % 100 == 0:
                self.stdout.write(
                    f'  Processed {processed:,}/{total_flights:,} flights... '
                    f'({updated_count:,} to update)',
                    ending='\r'
                )
                self.stdout.flush()
            
            # Bulk update in batches
            if not dry_run and len(flights_to_update) >= batch_size:
                with transaction.atomic():
                    Flight.objects.bulk_update(flights_to_update, ['airline_display_name'], batch_size=batch_size)
                self.stdout.write(
                    f'\n  ✓ Updated batch of {len(flights_to_update):,} flights'
                )
                flights_to_update = []
        
        # Update remaining flights
        if not dry_run and flights_to_update:
            with transaction.atomic():
                Flight.objects.bulk_update(flights_to_update, ['airline_display_name'], batch_size=batch_size)
            self.stdout.write(
                f'\n  ✓ Updated final batch of {len(flights_to_update):,} flights'
            )
        
        # Summary
        self.stdout.write('\n' + '='*60)
        self.stdout.write(self.style.SUCCESS('\nSUMMARY:'))
        self.stdout.write(f'  Total flights checked: {total_flights:,}')
        self.stdout.write(self.style.WARNING(f'  Flights to update: {updated_count:,}'))
        self.stdout.write(f'  Flights unchanged: {unchanged_count:,}')
        
        # Edge case: No flights needed updating
        if updated_count == 0:
            self.stdout.write(
                self.style.SUCCESS('\n✓ All flights already have airline_display_name populated! No updates needed.')
            )
            return
        
        if changes and verbose:
            self.stdout.write('\n' + self.style.SUCCESS('SAMPLE CHANGES (first 20):'))
            for change in changes:
                self.stdout.write(
                    f"  Flight #{change['id']} ({change['airline']}): {change['changes']}"
                )
            if updated_count > 20:
                self.stdout.write(f"  ... and {updated_count - 20:,} more changes")
        
        if dry_run:
            self.stdout.write(
                self.style.WARNING('\nThis was a dry run. Run without --dry-run to apply changes.')
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f'\n✓ Successfully populated airline_display_name for {updated_count:,} flight(s)!')
            )
