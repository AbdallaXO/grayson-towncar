"""
Django management command to normalize airline names in existing Flight records.

This command will update all Flight objects in the database to have normalized
airline codes (IATA format) regardless of how they were originally entered.

Optimized for large databases with batch processing and bulk updates.

Usage:
    python manage.py normalize_airlines
    
    # Dry run (show what would be changed without saving):
    python manage.py normalize_airlines --dry-run
    
    # Verbose output (show sample changes):
    python manage.py normalize_airlines --verbose
    
    # Custom batch size (default 500):
    python manage.py normalize_airlines --batch-size 1000
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from reservations.models import Flight
from reservations.utils import normalize_airline


class Command(BaseCommand):
    help = 'Normalize airline names in all existing Flight records to IATA codes'

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

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        verbose = options['verbose']
        batch_size = options['batch_size']
        
        # Get all flights with airline data using iterator for memory efficiency
        flights_queryset = Flight.objects.exclude(
            airline__isnull=True
        ).exclude(
            airline=''
        ).only('id', 'airline')  # Only fetch needed fields
        
        total_flights = flights_queryset.count()
        
        # Edge case: No flights with airline data
        if total_flights == 0:
            self.stdout.write(
                self.style.WARNING('\nNo flights found with airline data to normalize.\n')
            )
            self.stdout.write('This could mean:')
            self.stdout.write('  - No flights exist in the database')
            self.stdout.write('  - All flights have empty/null airline fields')
            return
        
        updated_count = 0
        unchanged_count = 0
        changes = []
        flights_to_update = []
        
        self.stdout.write(
            self.style.SUCCESS(f'\nFound {total_flights:,} flights with airline data to check.\n')
        )
        
        if dry_run:
            self.stdout.write(
                self.style.WARNING('DRY RUN MODE - No changes will be saved.\n')
            )
        
        self.stdout.write(f'Processing in batches of {batch_size:,}...\n')
        
        # Process flights in batches to avoid memory issues
        processed = 0
        for flight in flights_queryset.iterator(chunk_size=batch_size):
            original_airline = flight.airline
            normalized_airline = normalize_airline(original_airline)
            
            if original_airline != normalized_airline:
                updated_count += 1
                flight.airline = normalized_airline
                flights_to_update.append(flight)
                
                # Store sample changes for display
                if len(changes) < 20:
                    changes.append({
                        'id': flight.id,
                        'original': original_airline,
                        'normalized': normalized_airline,
                    })
                
                if verbose and len(changes) <= 20:
                    self.stdout.write(
                        f"Flight #{flight.id}: '{original_airline}' → '{normalized_airline}'"
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
                    Flight.objects.bulk_update(flights_to_update, ['airline'], batch_size=batch_size)
                self.stdout.write(
                    f'\n  ✓ Updated batch of {len(flights_to_update):,} flights'
                )
                flights_to_update = []
        
        # Update remaining flights
        if not dry_run and flights_to_update:
            with transaction.atomic():
                Flight.objects.bulk_update(flights_to_update, ['airline'], batch_size=batch_size)
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
                self.style.SUCCESS('\n✓ All flights are already normalized! No updates needed.')
            )
            return
        
        if changes and verbose:
            self.stdout.write('\n' + self.style.SUCCESS('SAMPLE CHANGES (first 20):'))
            for change in changes:
                self.stdout.write(
                    f"  Flight #{change['id']}: '{change['original']}' → '{change['normalized']}'"
                )
            if updated_count > 20:
                self.stdout.write(f"  ... and {updated_count - 20:,} more changes")
        
        if dry_run:
            self.stdout.write(
                self.style.WARNING('\nThis was a dry run. Run without --dry-run to apply changes.')
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f'\n✓ Successfully normalized {updated_count:,} flight(s)!')
            )

