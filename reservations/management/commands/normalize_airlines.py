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
from django.db.models import Q
from reservations.models import Flight
from reservations.utils import (
    normalize_airline, 
    normalize_flight_number, 
    extract_airline_from_flight_number
)


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
        
        # Get all flights with airline or flight_number data using iterator for memory efficiency
        flights_queryset = Flight.objects.filter(
            (Q(airline__isnull=False) & ~Q(airline='')) |
            (Q(flight_number__isnull=False) & ~Q(flight_number=''))
        ).only('id', 'airline', 'flight_number')  # Only fetch needed fields
        
        total_flights = flights_queryset.count()
        
        # Edge case: No flights with airline or flight_number data
        if total_flights == 0:
            self.stdout.write(
                self.style.WARNING('\nNo flights found with airline or flight_number data to normalize.\n')
            )
            self.stdout.write('This could mean:')
            self.stdout.write('  - No flights exist in the database')
            self.stdout.write('  - All flights have empty/null airline and flight_number fields')
            return
        
        updated_count = 0
        unchanged_count = 0
        changes = []
        flights_to_update = []
        
        self.stdout.write(
            self.style.SUCCESS(f'\nFound {total_flights:,} flights with airline or flight_number data to check.\n')
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
            original_flight_number = flight.flight_number or ""
            needs_update = False
            
            # Normalize airline first
            if original_airline:
                normalized_airline = normalize_airline(original_airline)
                if original_airline != normalized_airline:
                    flight.airline = normalized_airline
                    needs_update = True
            else:
                normalized_airline = None
            
            # Handle flight_number
            if original_flight_number:
                flight_upper = str(original_flight_number).strip().upper()
                new_flight_number = original_flight_number
                
                # If airline is set, check if flight_number starts with that airline code
                if flight.airline and len(flight.airline) == 2:
                    if flight_upper.startswith(flight.airline):
                        # Remove the airline code prefix
                        new_flight_number = flight_upper[len(flight.airline):]
                
                # If airline is empty, try to extract it from flight_number
                elif not flight.airline or flight.airline.strip() == "":
                    extracted_airline = extract_airline_from_flight_number(original_flight_number)
                    if extracted_airline:
                        flight.airline = normalize_airline(extracted_airline)
                        # Remove the airline code from flight_number
                        if flight_upper.startswith(extracted_airline):
                            new_flight_number = flight_upper[len(extracted_airline):]
                        needs_update = True
                
                # Clean the flight number (remove all letters, keep only digits)
                normalized_flight_number = normalize_flight_number(new_flight_number)
                if original_flight_number != normalized_flight_number:
                    flight.flight_number = normalized_flight_number
                    needs_update = True
            
            if needs_update:
                updated_count += 1
                flights_to_update.append(flight)
                
                # Store sample changes for display
                if len(changes) < 20:
                    change_desc = []
                    if original_airline != (flight.airline or ""):
                        change_desc.append(f"airline: '{original_airline}' → '{flight.airline}'")
                    if original_flight_number != (flight.flight_number or ""):
                        change_desc.append(f"flight_number: '{original_flight_number}' → '{flight.flight_number}'")
                    
                    if change_desc:
                        changes.append({
                            'id': flight.id,
                            'changes': ', '.join(change_desc),
                        })
                
                if verbose and len(changes) <= 20:
                    change_str = f"Flight #{flight.id}: "
                    if original_airline != (flight.airline or ""):
                        change_str += f"airline '{original_airline}' → '{flight.airline}'"
                    if original_flight_number != (flight.flight_number or ""):
                        if change_str != f"Flight #{flight.id}: ":
                            change_str += ", "
                        change_str += f"flight_number '{original_flight_number}' → '{flight.flight_number}'"
                    self.stdout.write(change_str)
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
                    Flight.objects.bulk_update(flights_to_update, ['airline', 'flight_number'], batch_size=batch_size)
                self.stdout.write(
                    f'\n  ✓ Updated batch of {len(flights_to_update):,} flights'
                )
                flights_to_update = []
        
        # Update remaining flights
        if not dry_run and flights_to_update:
            with transaction.atomic():
                Flight.objects.bulk_update(flights_to_update, ['airline', 'flight_number'], batch_size=batch_size)
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
                    f"  Flight #{change['id']}: {change['changes']}"
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

