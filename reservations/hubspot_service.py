import os
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

from hubspot import HubSpot
from hubspot.crm.contacts import SimplePublicObjectInput as ContactInput
from hubspot.crm.deals import SimplePublicObjectInput as DealInput

HUBSPOT_TOKEN = os.environ.get('HUBSPOT_TOKEN')
PIPELINE_ID = "default"  
DEAL_STAGE_ID = "appointmentscheduled"  

client = HubSpot(access_token=HUBSPOT_TOKEN)

def find_deal_by_reservation_id(reservation_id):
    """Find an existing deal by reservation_id property"""
    if not HUBSPOT_TOKEN:
        logger.error("HUBSPOT_TOKEN not found in environment variables")
        return None

    body = {
        "filterGroups": [
            {
                "filters": [
                    {
                        "propertyName": "reservation_id",
                        "operator": "EQ",
                        "value": str(reservation_id),
                    }
                ]
            }
        ]
    }
    try:
        resp = client.crm.deals.search_api.do_search(public_object_search_request=body)
        if resp.results:
            return resp.results[0].id
        return None
    except Exception as e:
        logger.error(f"Error searching for deal: {e}")
        return None


def create_or_find_contact(customer):
    """Find or create a contact in HubSpot"""
    if not HUBSPOT_TOKEN:
        logger.error("HUBSPOT_TOKEN not found in environment variables")
        return None

    # Search by email
    body = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": customer.email}
                ]
            }
        ]
    }

    try:
        resp = client.crm.contacts.search_api.do_search(
            public_object_search_request=body
        )
        if resp.results:
            logger.info(f"Found contact {resp.results[0].id}")
            return resp.results[0].id
    except Exception as e:
        logger.error(f"Error searching for contact: {e}")

    # Create new contact if not found
    try:
        props = {
            "email": customer.email,
            "firstname": customer.first_name,
            "lastname": customer.last_name,
            "phone": customer.phone_number,
            "zip": customer.zipcode,
        }

        new_ct = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=ContactInput(properties=props)
        )
        logger.info(f"Created contact {new_ct.id}")
        return new_ct.id
    except Exception as e:
        logger.error(f"Error creating contact: {e}")
        return None


def create_deal(reservation, contact_id):
    """Create a deal in HubSpot for the reservation"""
    if not HUBSPOT_TOKEN:
        logger.error("HUBSPOT_TOKEN not found in environment variables")
        return None

    # Gather all legs and sort
    legs = list(reservation.legs.order_by("pickup_date", "pickup_time"))
    if not legs:
        raise ValueError("Reservation has no legs.")

    first_leg = legs[0]
    last_leg = legs[-1] if len(legs) > 1 else first_leg

    # Timestamp for close date (first pickup)
    dt = datetime.combine(first_leg.pickup_date, first_leg.pickup_time).replace(
        tzinfo=timezone.utc
    )
    close_ms = int(dt.timestamp() * 1000)

    # Build location strings with flight info if available
    def with_flight(leg, addr, is_dropoff=False):
        if hasattr(leg, "flight_information") and leg.flight_information:
            flight = leg.flight_information
            label = "Drop-off" if is_dropoff else "Flight"
            flight_info = f"{flight.flight_number}" if flight.flight_number else ""
            if flight_info:
                return f"{addr} ({label}: {flight_info})"
        return addr

    pickup_loc = with_flight(first_leg, first_leg.pickup_location, is_dropoff=False)
    dropoff_loc = with_flight(last_leg, last_leg.dropoff_location, is_dropoff=True)

    # Get vehicle type
    vehicle_type = (
        reservation.rate.vehicle.vehicle_type.title()
        if hasattr(reservation.rate, "vehicle")
        else "Vehicle"
    )

    # Format trip type for better display
    trip_type_display = reservation.trip_type.replace("_", " ").title()

    # Build description text
    description_text = (
        f"**Trip Details**\n"
        f"- Vehicle: {vehicle_type}\n"
        f"- Trip Type: {trip_type_display}\n"
        f"- Passengers: {reservation.passenger_count}\n"
        f"- Luggage: {reservation.luggage_count}\n"
        f"- Reservation ID: {reservation.id}\n"
    )

    if hasattr(reservation, "display_carseats") and reservation.need_carseats:
        description_text += f"- Car Seats: {reservation.display_carseats()}\n"

    if reservation.special_requests:
        description_text += f"- Special Requests: {reservation.special_requests}\n"

    description_text += (
        f"\n**Leg 1**\n"
        f"- Pickup: {with_flight(first_leg, first_leg.pickup_location)} at {first_leg.pickup_time}\n"
        f"- Drop-off: {with_flight(first_leg, first_leg.dropoff_location)}\n"
    )

    if len(legs) > 1:
        description_text += (
            f"\n**Leg 2**\n"
            f"- Pickup: {with_flight(last_leg, last_leg.pickup_location)} at {last_leg.pickup_time}\n"
            f"- Drop-off: {with_flight(last_leg, last_leg.dropoff_location, is_dropoff=True)}\n"
        )

    # Deal properties
    deal_props = {
        "dealname": f"{vehicle_type} — {trip_type_display} - #{reservation.id}",
        "amount": float(
            reservation.total_price
        ),  # Convert Decimal to float for HubSpot
        "pipeline": PIPELINE_ID,
        "dealstage": DEAL_STAGE_ID,
        "closedate": close_ms,
        "reservation_id": str(reservation.id),
        "reservation_status": reservation.status,
        "description": description_text,
        "Payment Status": "Pending",  # Default payment status for new reservations
    }

    # Add travel agent info if available
    if hasattr(reservation, "travel_agent") and reservation.travel_agent:
        try:
            deal_props["travel_agent"] = reservation.travel_agent.user.get_full_name()
            if reservation.commission_amount:
                deal_props["commission_amount"] = float(reservation.commission_amount)
            deal_props["commission_paid"] = (
                "Yes" if reservation.commission_paid else "No"
            )
        except:
            pass  # Skip travel agent info if there's an error

    try:
        logger.info(f"Creating deal with properties: {deal_props}")
        deal = client.crm.deals.basic_api.create(
            simple_public_object_input_for_create=DealInput(properties=deal_props)
        )
        deal_id = deal.id
        logger.info(f"Deal created {deal_id}")

        # Associate contact ↔ deal
        try:
            client.crm.associations.batch_api.create(
                from_object_type="deals",
                to_object_type="contacts",
                batch_input_public_association={
                    "inputs": [
                        {
                            "from": {"id": deal_id},
                            "to": {"id": contact_id},
                            "type": "deal_to_contact",
                        }
                    ]
                },
            )
            logger.info(f"Associated deal {deal_id} ↔ contact {contact_id}")
        except Exception as e:
            logger.error(f"Failed to create association: {str(e)}")

        return deal_id
    except Exception as e:
        logger.error(f"Error creating deal: {e}")
        return None


def sync_reservation_to_hubspot(reservation):
    """Main function to sync a reservation to HubSpot"""
    if not HUBSPOT_TOKEN:
        logger.warning(
            "HUBSPOT_TOKEN not found in environment variables - skipping HubSpot sync"
        )
        return {"success": False, "error": "HUBSPOT_TOKEN not found"}

    try:
        cust = reservation.customer
        logger.info(f"Syncing reservation #{reservation.id} for {cust.get_full_name()}")

        # Check if deal already exists for this reservation
        existing_deal_id = find_deal_by_reservation_id(reservation.id)
        if existing_deal_id:
            logger.info(
                f"Found existing deal {existing_deal_id} for reservation #{reservation.id}"
            )
            return {
                "success": True,
                "deal_id": existing_deal_id,
                "status": "existing",
            }

        # Create or find contact
        contact_id = create_or_find_contact(cust)
        if not contact_id:
            logger.error(f"Failed to create or find contact for customer {cust.id}")
            return {"success": False, "error": "Failed to create or find contact"}

        logger.info(f"Using contact {contact_id} for customer {cust.get_full_name()}")

        # Create deal
        deal_id = create_deal(reservation, contact_id)
        if not deal_id:
            logger.error(f"Failed to create deal for reservation {reservation.id}")
            return {"success": False, "error": "Failed to create deal"}

        logger.info(f"Created deal {deal_id} for reservation #{reservation.id}")
        logger.info(
            f"✅ Successfully synced reservation #{reservation.id} → deal {deal_id}"
        )

        return {
            "success": True,
            "contact_id": contact_id,
            "deal_id": deal_id,
            "status": "created",
        }
    except Exception as e:
        logger.error(
            f"Failed to sync reservation #{reservation.id} to HubSpot: {str(e)}"
        )
        return {"success": False, "error": str(e)}


def update_deal_payment_status(
    reservation_id, payment_status, payment_amount=None, payment_method=None
):
    """
    Update a HubSpot deal with payment information

    Args:
        reservation_id: ID of the reservation
        payment_status: Status of payment (e.g., "Paid", "Card On File", "Pending", "Failed")
        payment_amount: Amount paid (optional)
        payment_method: Method of payment (optional)
    """
    if not HUBSPOT_TOKEN:
        logger.warning(
            "HUBSPOT_TOKEN not found in environment variables - skipping HubSpot sync"
        )
        return {"success": False, "error": "HUBSPOT_TOKEN not found"}

    # Find the deal ID for this reservation
    deal_id = find_deal_by_reservation_id(reservation_id)
    if not deal_id:
        logger.error(f"No HubSpot deal found for reservation #{reservation_id}")
        return {"success": False, "error": "Deal not found"}

    # Update the deal with payment information
    try:
        # Use exact property names as defined in HubSpot
        props = {
            "payment_status": payment_status,
        }

        # Add payment amount if provided
        if payment_amount is not None:
            props["payment_amount"] = float(payment_amount)

        # Add payment method if provided
        if payment_method:
            props["payment_method"] = payment_method

        # Update the deal
        client.crm.deals.basic_api.update(
            deal_id=deal_id, simple_public_object_input=DealInput(properties=props)
        )

        logger.info(f"Updated payment status to {payment_status} for deal {deal_id}")
        return {"success": True, "deal_id": deal_id}

    except Exception as e:
        logger.error(f"Error updating deal payment status: {e}")
        return {"success": False, "error": str(e)}


def update_reservation_payment(reservation_id, **kwargs):
    """
    Update payment details for a reservation

    Args:
        reservation_id: ID of the reservation
        **kwargs: Can include:
            - status: Payment status (Paid, Pending, Failed, etc.)
            - amount: Payment amount
            - method: Payment method (e.g., "Visa ending in 4242")
    """
    if not HUBSPOT_TOKEN:
        logger.warning("HUBSPOT_TOKEN not found - skipping HubSpot update")
        return {"success": False, "error": "HUBSPOT_TOKEN not found"}

    # Find the deal
    deal_id = find_deal_by_reservation_id(reservation_id)
    if not deal_id:
        logger.error(f"No HubSpot deal found for reservation #{reservation_id}")
        return {"success": False, "error": "Deal not found"}

    # Build properties to update
    props = {}

    if "status" in kwargs:
        props["Payment Status"] = kwargs["status"]

    if "amount" in kwargs and kwargs["amount"] is not None:
        props["Payment Amount"] = float(kwargs["amount"])

    if "method" in kwargs and kwargs["method"]:
        props["Payment Method"] = kwargs["method"]

    # Only proceed if we have properties to update
    if not props:
        logger.warning("No payment properties to update")
        return {"success": False, "error": "No properties to update"}

    try:
        # Update the deal
        client.crm.deals.basic_api.update(
            deal_id=deal_id, simple_public_object_input=DealInput(properties=props)
        )

        logger.info(f"Updated payment information for deal {deal_id}")
        return {"success": True, "deal_id": deal_id}
    except Exception as e:
        logger.error(f"Error updating payment information: {e}")
        return {"success": False, "error": str(e)}
