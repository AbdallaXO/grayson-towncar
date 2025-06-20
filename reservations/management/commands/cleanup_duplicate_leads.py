from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from datetime import timedelta
from reservations.models import Lead
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Clean up duplicate leads by merging them based on email and phone'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without actually doing it',
        )
        parser.add_argument(
            '--days',
            type=int,
            default=30,
            help='Look for duplicates within the last N days (default: 30)',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        days = options['days']
        cutoff_date = timezone.now() - timedelta(days=days)
        
        self.stdout.write(
            self.style.SUCCESS(f'Looking for duplicate leads in the last {days} days...')
        )
        
        # Get all leads within the time period
        recent_leads = Lead.objects.filter(created_at__gte=cutoff_date).order_by('created_at')
        
        duplicates_found = 0
        leads_merged = 0
        
        # Group leads by email and phone
        email_groups = {}
        phone_groups = {}
        
        for lead in recent_leads:
            # Group by email
            if lead.email:
                email = lead.email.lower().strip()
                if email not in email_groups:
                    email_groups[email] = []
                email_groups[email].append(lead)
            
            # Group by phone
            if lead.phone:
                phone = lead.phone.strip()
                if phone not in phone_groups:
                    phone_groups[phone] = []
                phone_groups[phone].append(lead)
        
        # Process email duplicates
        for email, leads in email_groups.items():
            if len(leads) > 1:
                duplicates_found += len(leads) - 1
                if not dry_run:
                    merged_count = self.merge_duplicate_leads(leads)
                    leads_merged += merged_count
                else:
                    self.stdout.write(
                        f'Would merge {len(leads)} leads with email: {email}'
                    )
        
        # Process phone duplicates
        for phone, leads in phone_groups.items():
            if len(leads) > 1:
                duplicates_found += len(leads) - 1
                if not dry_run:
                    merged_count = self.merge_duplicate_leads(leads)
                    leads_merged += merged_count
                else:
                    self.stdout.write(
                        f'Would merge {len(leads)} leads with phone: {phone}'
                    )
        
        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f'DRY RUN: Found {duplicates_found} potential duplicates to merge'
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f'Successfully merged {leads_merged} duplicate leads'
                )
            )

    def merge_duplicate_leads(self, leads):
        """
        Merge duplicate leads, keeping the oldest one and updating it with missing information
        """
        if len(leads) <= 1:
            return 0
        
        # Sort by creation date (oldest first)
        leads = sorted(leads, key=lambda x: x.created_at)
        primary_lead = leads[0]  # Keep the oldest lead
        leads_to_delete = leads[1:]  # Delete the newer duplicates
        
        # Update primary lead with missing information from duplicates
        updated = False
        for duplicate in leads_to_delete:
            if not primary_lead.pickup_location and duplicate.pickup_location:
                primary_lead.pickup_location = duplicate.pickup_location
                updated = True
            if not primary_lead.dropoff_location and duplicate.dropoff_location:
                primary_lead.dropoff_location = duplicate.dropoff_location
                updated = True
            if not primary_lead.vehicle and duplicate.vehicle:
                primary_lead.vehicle = duplicate.vehicle
                updated = True
            if not primary_lead.estimated_price and duplicate.estimated_price:
                primary_lead.estimated_price = duplicate.estimated_price
                updated = True
            if not primary_lead.pickup_date and duplicate.pickup_date:
                primary_lead.pickup_date = duplicate.pickup_date
                updated = True
            if not primary_lead.trip_type and duplicate.trip_type:
                primary_lead.trip_type = duplicate.trip_type
                updated = True
            if not primary_lead.notes and duplicate.notes:
                primary_lead.notes = duplicate.notes
                updated = True
        
        if updated:
            primary_lead.save()
        
        # Delete the duplicate leads
        for duplicate in leads_to_delete:
            duplicate.delete()
        
        self.stdout.write(
            f'Merged {len(leads_to_delete)} duplicates into lead ID {primary_lead.id}'
        )
        
        return len(leads_to_delete) 