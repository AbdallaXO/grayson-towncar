# test_hubspot.py
import os
import logging
from datetime import datetime, time, timezone
from decimal import Decimal
from hubspot.crm.associations import BatchInputPublicObjectId, PublicObjectId
# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# HubSpot client setup
from hubspot import HubSpot
from hubspot.crm.contacts import SimplePublicObjectInput as ContactInput
from hubspot.crm.deals import SimplePublicObjectInput as DealInput

# Configuration
HUBSPOT_TOKEN = os.environ.get('HUBSPOT_TOKEN')
PIPELINE_ID = "default"
DEAL_STAGE_ID = "appointmentscheduled"
DEAL_TO_CONTACT_ASSOCIATION_TYPE = 1  # Standard deal-to-contact association type ID
DRY_RUN = False  # Set to True for testing without making actual API calls

# Initialize HubSpot client
client = HubSpot(access_token=HUBSPOT_TOKEN)

# Mock classes for testing
class MockCustomer:
    def __init__(self, first_name, last_name, email, phone_number):
        self.first_name = first_name
        self.last_name = last_name
        self.email = email
        self.phone_number = phone_number
        self.hubspot_contact_id = None  # Add this to track the HubSpot contact ID
    def get_full_name(self):
        return f"{self.first_name} {self.last_name}"

class MockRate:
    def __init__(self, vehicle):
        self.vehicle = vehicle

class MockFlightInfo:
    def __init__(self, flight_number):
        self.flight_number = flight_number

class MockLeg:
    def __init__(self, pickup_date, pickup_time, pickup_location, dropoff_location, flight_information=None):
        self.pickup_date = pickup_date
        self.pickup_time = pickup_time
        self.pickup_location = pickup_location
        self.dropoff_location = dropoff_location
        self.flight_information = flight_information

class MockLegsQuerySet:
    def __init__(self, legs):
        self.legs = legs
    
    def order_by(self, *args):
        return self.legs

class MockReservation:
    def __init__(self, id, customer, rate, passenger_count, total_price, legs, created_at=None, status="confirmed"):
        self.id = id
        self.customer = customer
        self.rate = rate
        self.passenger_count = passenger_count
        self.total_price = total_price
        self.legs = MockLegsQuerySet(legs)
        self.created_at = created_at or datetime.now(timezone.utc)
        self.status = status
        self.hubspot_deal_id = None
    
    def save(self):
        print(f"Saving reservation {self.id} with hubspot_deal_id = {self.hubspot_deal_id}")

# Helper functions
def find_deal_by_reservation_id(reservation_id):
    """Find an existing deal by reservation_id property"""
    if DRY_RUN:
        logger.info(f"DRY RUN: Would search for deal with reservation_id = {reservation_id}")
        return None
    
    body = {
        "filterGroups": [{"filters": [
            {"propertyName": "reservation_id", "operator": "EQ", "value": str(reservation_id)}
        ]}]
    }
    resp = client.crm.deals.search_api.do_search(public_object_search_request=body)
    if resp.results:
        return resp.results[0].id
    return None

def create_or_find_contact(customer):
    """Find or create a contact in HubSpot"""
    if DRY_RUN:
        logger.info(f"DRY RUN: Would search for contact with email = {customer.email}")
        return "dry-run-contact-id"
    
    # search by email
    body = {
        "filterGroups": [{"filters": [
            {"propertyName": "email", "operator": "EQ", "value": customer.email}
        ]}]
    }
    resp = client.crm.contacts.search_api.do_search(public_object_search_request=body)
    if resp.results:
        logger.info(f"Found contact {resp.results[0].id}")
        return resp.results[0].id

    # create new contact
    props = {
        "email": customer.email,
        "firstname": customer.first_name,
        "lastname": customer.last_name,
        "phone": customer.phone_number,
        "company": "Individual Customer",
    }
    new_ct = client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=ContactInput(properties=props)
    )
    logger.info(f"Created contact {new_ct.id}")
    return new_ct.id

def create_deal(reservation, contact_id):
    """Create a deal in HubSpot for the reservation"""
    # gather all legs and sort
    legs = list(reservation.legs.order_by("pickup_date", "pickup_time"))
    if not legs:
        raise ValueError("Reservation has no legs.")
    first_leg = legs[0]
    last_leg = legs[-1]

    # timestamp for close date (first pickup)
    dt = datetime.combine(first_leg.pickup_date, first_leg.pickup_time).replace(tzinfo=timezone.utc)
    close_ms = int(dt.timestamp() * 1000)

    # build location strings
    def with_flight(leg, addr, is_dropoff=False):
        if leg.flight_information:
            label = "Drop-off" if is_dropoff else "Flight"
            return f"{addr} ({label}: {leg.flight_information.flight_number})"
        return addr

    pickup_loc = with_flight(first_leg, first_leg.pickup_location, is_dropoff=False)
    dropoff_loc = with_flight(last_leg, last_leg.dropoff_location, is_dropoff=True)

    # deal properties
    deal_props = {
        "dealname": f"{reservation.rate.vehicle} — {pickup_loc} → {dropoff_loc}",
        "amount": float(reservation.total_price),   # Convert Decimal to float for HubSpot
        "pipeline": PIPELINE_ID,
        "dealstage": DEAL_STAGE_ID,
        "closedate": close_ms,
        "reservation_id": str(reservation.id),  # Add reservation ID for reference
        "description": (
            f"**Trip Details**\n"
            f"- Vehicle: {reservation.rate.vehicle}\n"
            f"- Passengers: {reservation.passenger_count}\n"
            f"- Reservation ID: {reservation.id}\n\n"
            f"**Leg 1**\n"
            f"- Pickup: {with_flight(first_leg, first_leg.pickup_location)} at {first_leg.pickup_time}\n"
            f"- Drop-off: {with_flight(first_leg, first_leg.dropoff_location)}\n"
            + (
                f"\n**Leg 2**\n"
                f"- Pickup: {with_flight(last_leg, last_leg.pickup_location)} at {last_leg.pickup_time}\n"
                f"- Drop-off: {with_flight(last_leg, last_leg.dropoff_location, is_dropoff=True)}\n"
                if len(legs) == 2 else ""
            )
        )
    }

    if DRY_RUN:
        logger.info(f"DRY RUN: Would create deal with properties: {deal_props}")
        return "dry-run-deal-id"

    logger.info(f"Creating deal with properties: {deal_props}")
    try:
        deal = client.crm.deals.basic_api.create(
            simple_public_object_input_for_create=DealInput(properties=deal_props)
        )
    except Exception as e:
        if hasattr(e, "response"):
            logger.error(f"HubSpot status: {e.response.status_code}")
            logger.error(f"HubSpot body: {e.response.text}")
        raise

    logger.info(f"Deal created {deal.id}")

    # associate contact ↔ deal
    try:
        # Create association between deal and contact
        client.crm.associations.batch_api.create(
            from_object_type="deals",
            to_object_type="contacts",
            batch_input_public_association={
                "inputs": [
                    {
                        "from": {"id": deal.id},
                        "to": {"id": contact_id},
                        "type": "deal_to_contact"
                    }
                ]
            }
        )
        logger.info(f"Associated deal {deal.id} ↔ contact {contact_id}")
    except Exception as e:
        logger.error(f"Failed to create association: {str(e)}")
        # Continue even if association fails
        
    return deal.id

def sync_reservation_to_hubspot(reservation):
    """Main function to sync a reservation to HubSpot"""
    try:
        cust = reservation.customer
        logger.info(f"Syncing reservation #{reservation.id} for {cust.get_full_name()}")

        # Check if deal already exists for this reservation
        existing_deal_id = find_deal_by_reservation_id(reservation.id)
        if existing_deal_id:
            logger.info(f"Found existing deal {existing_deal_id} for reservation #{reservation.id}")
            return {"success": True, "contact_id": None, "deal_id": existing_deal_id, "status": "existing"}

        # Create or find contact
        contact_id = create_or_find_contact(cust)
        logger.info(f"Using contact {contact_id} for customer {cust.get_full_name()}")

        # Create deal
        deal_id = create_deal(reservation, contact_id)
        logger.info(f"Created deal {deal_id} for reservation #{reservation.id}")

        # Store deal ID in the reservation if your model supports this
        if hasattr(reservation, 'hubspot_deal_id'):
            reservation.hubspot_deal_id = deal_id
            reservation.save()
            logger.info(f"Updated reservation #{reservation.id} with HubSpot deal ID {deal_id}")

        logger.info(f"✅ Successfully synced reservation #{reservation.id} → deal {deal_id}")
        return {"success": True, "contact_id": contact_id, "deal_id": deal_id, "status": "created"}
    except Exception as e:
        logger.error(f"Failed to sync reservation #{reservation.id} to HubSpot: {str(e)}")
        return {"success": False, "error": str(e)}

# Create test data
def create_test_reservation():
    customer = MockCustomer("Test", "User", "test.user.new@example.com", "+1987654321")
    rate = MockRate("Premium SUV")
    flight_info = MockFlightInfo("DL5678")
    
    today = datetime.now(timezone.utc).date()
    pickup_time = time(15, 45)  # 3:45 PM
    
    legs = [
        MockLeg(
            pickup_date=today,
            pickup_time=pickup_time,
            pickup_location="456 Park Ave, New York",
            dropoff_location="JFK Airport Terminal 4",
            flight_information=flight_info
        )
    ]
    
    return MockReservation(
        id=88888,  # Changed to a new ID to ensure no existing deal
        customer=customer,
        rate=rate,
        passenger_count=4,
        total_price=Decimal("250.00"),
        legs=legs,
        status="confirmed"
    )

# Run the test
if __name__ == "__main__":
    print("Starting HubSpot integration test...")
    
    # Set DRY_RUN to False to actually make API calls
    DRY_RUN = False
    
    # Create test reservation
    reservation = create_test_reservation()
    
    # Test finding existing deal
    existing_deal = find_deal_by_reservation_id(reservation.id)
    print(f"Existing deal for reservation #{reservation.id}: {existing_deal}")
    
    # Test full sync
    result = sync_reservation_to_hubspot(reservation)
    print("Sync result:", result)
    
    print("Test completed.")