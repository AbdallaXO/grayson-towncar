from django.core.management.base import BaseCommand
from django.db.models import Count, Q
from reservations.models import Lead, Quote
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Merge duplicate leads by email and phone, preserving all quotes'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without actually doing it',
        )
        parser.add_argument(
            '--by-email',
            action='store_true',
            help='Merge duplicates by email address',
        )
        parser.add_argument(
            '--by-phone',
            action='store_true',
            help='Merge duplicates by phone number',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        by_email = options['by_email']
        by_phone = options['by_phone']
        
        # If no specific method chosen, do both
        if not by_email and not by_phone:
            by_email = True
            by_phone = True
        
        total_merged = 0
        
        if by_email:
            email_merged = self.merge_by_email(dry_run)
            total_merged += email_merged
            
        if by_phone:
            phone_merged = self.merge_by_phone(dry_run)
            total_merged += phone_merged
        
        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f'DRY RUN: Would merge {total_merged} duplicate leads'
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f'Successfully merged {total_merged} duplicate leads'
                )
            )

    def merge_by_email(self, dry_run=False):
        """Merge leads with the same email address"""
        merged_count = 0
        
        # Find all emails with more than one lead
        dupe_emails = (
            Lead.objects.exclude(email='')
            .values('email')
            .annotate(count=Count('id'))
            .filter(count__gt=1)
            .values_list('email', flat=True)
        )
        
        for email in dupe_emails:
            leads = Lead.objects.filter(email=email).order_by('created_at')
            main_lead = leads.first()  # Keep the oldest lead
            duplicates = leads.exclude(id=main_lead.id)
            
            if dry_run:
                self.stdout.write(f'Would merge {duplicates.count()} leads with email: {email}')
                continue
            
            # Move all quotes from duplicates to main lead
            for duplicate in duplicates:
                Quote.objects.filter(lead=duplicate).update(lead=main_lead)
                
                # Merge notes if main lead doesn't have notes
                if not main_lead.notes and duplicate.notes:
                    main_lead.notes = duplicate.notes
                
                # Update main lead with any missing info from duplicate
                if not main_lead.phone and duplicate.phone:
                    main_lead.phone = duplicate.phone
                if not main_lead.last_name and duplicate.last_name:
                    main_lead.last_name = duplicate.last_name
                
                # Delete the duplicate
                duplicate.delete()
                merged_count += 1
            
            # Save the updated main lead
            main_lead.save()
            
            self.stdout.write(f'Merged {duplicates.count()} leads with email: {email}')
        
        return merged_count

    def merge_by_phone(self, dry_run=False):
        """Merge leads with the same phone number"""
        merged_count = 0
        
        # Find all phones with more than one lead
        dupe_phones = (
            Lead.objects.exclude(phone='')
            .values('phone')
            .annotate(count=Count('id'))
            .filter(count__gt=1)
            .values_list('phone', flat=True)
        )
        
        for phone in dupe_phones:
            leads = Lead.objects.filter(phone=phone).order_by('created_at')
            main_lead = leads.first()  # Keep the oldest lead
            duplicates = leads.exclude(id=main_lead.id)
            
            if dry_run:
                self.stdout.write(f'Would merge {duplicates.count()} leads with phone: {phone}')
                continue
            
            # Move all quotes from duplicates to main lead
            for duplicate in duplicates:
                Quote.objects.filter(lead=duplicate).update(lead=main_lead)
                
                # Merge notes if main lead doesn't have notes
                if not main_lead.notes and duplicate.notes:
                    main_lead.notes = duplicate.notes
                
                # Update main lead with any missing info from duplicate
                if not main_lead.email and duplicate.email:
                    main_lead.email = duplicate.email
                if not main_lead.last_name and duplicate.last_name:
                    main_lead.last_name = duplicate.last_name
                
                # Delete the duplicate
                duplicate.delete()
                merged_count += 1
            
            # Save the updated main lead
            main_lead.save()
            
            self.stdout.write(f'Merged {duplicates.count()} leads with phone: {phone}')
        
        return merged_count 