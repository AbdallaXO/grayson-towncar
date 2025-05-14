from django.core.management.base import BaseCommand
from reservations.models import Reservation, Customer
from payment.models import Payment
from reservations.hubspot_service import (
    sync_reservation_to_hubspot, 
    update_deal_payment_status,
    create_or_find_contact
)
import logging
import time

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Sync all existing customers, reservations, and payments to HubSpot'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Run without making actual API calls',
        )
        parser.add_argument(
            '--limit',
            type=int,
            help='Limit the number of records to process',
        )
        parser.add_argument(
            '--customers-only',
            action='store_true',
            help='Only sync customers',
        )
        parser.add_argument(
            '--reservations-only',
            action='store_true',
            help='Only sync reservations',
        )
        parser.add_argument(
            '--payments-only',
            action='store_true',
            help='Only sync payments',
        )
    
    def handle(self, *args, **options):
        dry_run = options['dry_run']
        limit = options.get('limit')
        
        # Determine what to sync based on options
        sync_customers = not (options['reservations_only'] or options['payments_only'])
        sync_reservations = not (options['customers_only'] or options['payments_only'])
        sync_payments = not (options['customers_only'] or options['reservations_only'])
        
        if sync_customers:
            self.sync_customers(dry_run, limit)
        
        if sync_reservations:
            self.sync_reservations(dry_run, limit)
        
        if sync_payments:
            self.sync_payments(dry_run, limit)
        
        self.stdout.write(self.style.SUCCESS('Import completed!'))
    
    def sync_customers(self, dry_run, limit):
        """Sync all customers to HubSpot"""
        self.stdout.write("Starting customer sync...")
        
        queryset = Customer.objects.all()
        if limit:
            queryset = queryset[:limit]
        
        total = queryset.count()
        self.stdout.write(f"Found {total} customers to sync")
        
        success = 0
        for i, customer in enumerate(queryset):
            self.stdout.write(f"Processing customer {i+1}/{total}: {customer.get_full_name()}")
            
            if not dry_run:
                try:
                    contact_id = create_or_find_contact(customer)
                    if contact_id:
                        success += 1
                        self.stdout.write(f"  ✅ Synced to HubSpot contact ID: {contact_id}")
                    else:
                        self.stdout.write(self.style.ERROR(f"  ❌ Failed to sync"))
                    
                    # Add a small delay to avoid rate limits
                    time.sleep(0.2)
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"  ❌ Error: {str(e)}"))
        
        self.stdout.write(self.style.SUCCESS(f"Completed customer sync: {success}/{total} successful"))
    
    def sync_reservations(self, dry_run, limit):
        """Sync all reservations to HubSpot"""
        self.stdout.write("Starting reservation sync...")
        
        queryset = Reservation.objects.select_related('customer', 'rate', 'rate__vehicle').prefetch_related('legs')
        if limit:
            queryset = queryset[:limit]
        
        total = queryset.count()
        self.stdout.write(f"Found {total} reservations to sync")
        
        success = 0
        for i, reservation in enumerate(queryset):
            self.stdout.write(f"Processing reservation {i+1}/{total}: #{reservation.id}")
            
            if not dry_run:
                try:
                    result = sync_reservation_to_hubspot(reservation)
                    if result.get('success'):
                        success += 1
                        deal_id = result.get('deal_id')
                        status = result.get('status')
                        self.stdout.write(f"  ✅ Synced to HubSpot deal ID: {deal_id} ({status})")
                    else:
                        self.stdout.write(self.style.ERROR(f"  ❌ Failed to sync: {result.get('error')}"))
                    
                    # Add a small delay to avoid rate limits
                    time.sleep(0.2)
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"  ❌ Error: {str(e)}"))
        
        self.stdout.write(self.style.SUCCESS(f"Completed reservation sync: {success}/{total} successful"))
    
    def sync_payments(self, dry_run, limit):
        """Sync all payments to HubSpot"""
        self.stdout.write("Starting payment sync...")
        
        queryset = Payment.objects.select_related('reservation', 'customer').filter(reservation__isnull=False)
        if limit:
            queryset = queryset[:limit]
        
        total = queryset.count()
        self.stdout.write(f"Found {total} payments to sync")
        
        success = 0
        for i, payment in enumerate(queryset):
            self.stdout.write(f"Processing payment {i+1}/{total}: {payment}")
            
            if not dry_run:
                try:
                    # Map payment status to HubSpot status
                    status_map = {
                        "pending": "Pending",
                        "card_saved": "Card On File",
                        "paid": "Paid",
                        "failed": "Failed"
                    }
                    hubspot_status = status_map.get(payment.status, "Unknown")
                    
                    # Get payment method if available
                    payment_method = None
                    if payment.customer and hasattr(payment.customer, 'card_brand') and payment.customer.card_brand and payment.customer.card_last4:
                        payment_method = f"{payment.customer.card_brand.title()} ending in {payment.customer.card_last4}"
                    
                    # Update HubSpot
                    result = update_deal_payment_status(
                        reservation_id=payment.reservation.id,
                        payment_status=hubspot_status,
                        payment_amount=payment.amount,
                        payment_method=payment_method
                    )
                    
                    if result.get('success'):
                        success += 1
                        self.stdout.write(f"  ✅ Updated payment status in HubSpot")
                    else:
                        self.stdout.write(self.style.ERROR(f"  ❌ Failed to update: {result.get('error')}"))
                    
                    # Add a small delay to avoid rate limits
                    time.sleep(0.2)
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"  ❌ Error: {str(e)}"))
        
        self.stdout.write(self.style.SUCCESS(f"Completed payment sync: {success}/{total} successful"))