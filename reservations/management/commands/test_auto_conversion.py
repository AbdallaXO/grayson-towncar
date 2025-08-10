from django.core.management.base import BaseCommand
from django.utils import timezone
from reservations.models import Lead, Reservation, Customer
from rates.models import Rate, Vehicle


class Command(BaseCommand):
    help = 'Test the automatic lead conversion system'

    def add_arguments(self, parser):
        parser.add_argument(
            '--create-test-data',
            action='store_true',
            help='Create test leads and reservations to test conversion',
        )
        parser.add_argument(
            '--check-conversions',
            action='store_true',
            help='Check which leads should be converted based on existing reservations',
        )

    def handle(self, *args, **options):
        if options['create_test_data']:
            self.create_test_data()
        elif options['check_conversions']:
            self.check_conversions()
        else:
            self.stdout.write(
                self.style.WARNING(
                    'Please specify --create-test-data or --check-conversions'
                )
            )

    def create_test_data(self):
        """Create test leads and reservations to test the conversion system"""
        self.stdout.write('Creating test data...')
        
        # Create a test vehicle and rate
        vehicle, created = Vehicle.objects.get_or_create(
            vehicle_type='towncar',
            defaults={
                'name': 'Test Town Car',
                'capacity': 4,
                'description': 'Test vehicle for auto-conversion'
            }
        )
        
        rate, created = Rate.objects.get_or_create(
            vehicle=vehicle,
            defaults={
                'base_price': 100.00,
                'description': 'Test rate for auto-conversion'
            }
        )
        
        # Create test leads
        test_emails = ['test1@example.com', 'test2@example.com', 'test3@example.com']
        test_phones = ['555-0101', '555-0102', '555-0103']
        
        for i, (email, phone) in enumerate(zip(test_emails, test_phones)):
            lead, created = Lead.objects.get_or_create(
                email=email,
                defaults={
                    'first_name': f'Test{i+1}',
                    'last_name': 'Lead',
                    'phone': phone,
                    'pickup_location': 'Test Pickup',
                    'dropoff_location': 'Test Dropoff',
                    'status': 'interested',
                    'priority': 'medium'
                }
            )
            if created:
                self.stdout.write(f'Created test lead: {lead}')
            else:
                self.stdout.write(f'Test lead already exists: {lead}')
        
        # Create test customers and reservations (this will trigger auto-conversion)
        for i, (email, phone) in enumerate(zip(test_emails, test_phones)):
            customer, created = Customer.objects.get_or_create(
                email=email,
                defaults={
                    'first_name': f'Test{i+1}',
                    'last_name': 'Customer',
                    'phone_number': phone,
                    'zipcode': '12345'
                }
            )
            
            if created:
                reservation = Reservation.objects.create(
                    customer=customer,
                    rate=rate,
                    vehicle=vehicle,
                    trip_type='oneway',
                    passenger_count=2,
                    luggage_count=2,
                    base_price=100.00,
                    additional_charges=0.00,
                    total_price=100.00,
                    status='confirmed'
                )
                self.stdout.write(f'Created test reservation: {reservation}')
            else:
                self.stdout.write(f'Test customer already exists: {customer}')
        
        self.stdout.write(
            self.style.SUCCESS('Test data created successfully!')
        )

    def check_conversions(self):
        """Check which leads should be converted based on existing reservations"""
        self.stdout.write('Checking for leads that should be auto-converted...')
        
        converted_count = 0
        for lead in Lead.objects.filter(status__in=['new', 'contacted', 'interested', 'future_contact']):
            # Check if there's a reservation with matching email or phone
            matching_reservation = None
            
            if lead.email:
                matching_reservation = Reservation.objects.filter(
                    customer__email__iexact=lead.email
                ).first()
            
            if not matching_reservation and lead.phone:
                matching_reservation = Reservation.objects.filter(
                    customer__phone_number__iexact=lead.phone
                ).first()
            
            if matching_reservation:
                self.stdout.write(
                    f'Lead {lead.id} ({lead.first_name} {lead.last_name}) should be converted - '
                    f'matches Reservation #{matching_reservation.id}'
                )
                converted_count += 1
        
        if converted_count == 0:
            self.stdout.write(
                self.style.SUCCESS('No leads need conversion!')
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f'Found {converted_count} leads that should be converted. '
                    f'Use the "Check for Auto-Conversion" admin action to convert them.'
                )
            )
