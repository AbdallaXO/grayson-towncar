import os
import logging
from datetime import datetime, timezone
from functools import wraps
from django.core.cache import cache

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Set this to False if HubSpot imports fail
HUBSPOT_AVAILABLE = False
HUBSPOT_TOKEN = os.environ.get("HUBSPOT_TOKEN")
PIPELINE_ID = os.environ.get("HUBSPOT_PIPELINE_ID", "default")
DEAL_STAGE_ID = os.environ.get("HUBSPOT_DEAL_STAGE_ID", "appointmentscheduled")

# Try to import HubSpot, but catch import errors
try:
    from hubspot import HubSpot
    from hubspot.crm.contacts import SimplePublicObjectInput as ContactInput
    from hubspot.crm.deals import SimplePublicObjectInput as DealInput

    # Only create the client if token exists
    if HUBSPOT_TOKEN:
        client = HubSpot(access_token=HUBSPOT_TOKEN)
        HUBSPOT_AVAILABLE = True
    else:
        logger.warning("HUBSPOT_TOKEN not found, HubSpot integration disabled")
except ImportError as e:
    logger.warning(f"HubSpot import failed: {e}. HubSpot integration disabled.")

# Cache timeouts
CACHE_TIMEOUT = {
    "deal": 60 * 60 * 12,  # 12 hours for deals
    "contact": 60 * 60 * 24,  # 24 hours for contacts
}


# Retry decorator for transient errors
def with_retries(max_attempts=3, retry_on=(Exception,)):
    """Decorator to retry functions on certain exceptions"""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempts = 0
            last_error = None

            while attempts < max_attempts:
                try:
                    return func(*args, **kwargs)
                except retry_on as e:
                    attempts += 1
                    last_error = e
                    if attempts < max_attempts:
                        # Exponential backoff: 1s, 2s, 4s, etc.
                        wait_time = 2 ** (attempts - 1)
                        logger.warning(
                            f"Retrying {func.__name__} after error: {e}. "
                            f"Attempt {attempts}/{max_attempts} in {wait_time}s"
                        )
                        import time

                        time.sleep(wait_time)

            # If we get here, all retries failed
            logger.error(
                f"All {max_attempts} attempts failed for {func.__name__}: {last_error}"
            )
            raise last_error

        return wrapper

    return decorator


def find_deal_by_reservation_id(reservation_id, use_cache=True):
    """Find an existing deal by reservation_id property with caching"""
    if not HUBSPOT_AVAILABLE or not HUBSPOT_TOKEN:
        logger.info("HubSpot integration not available - skipping deal lookup")
        return None

    # Check cache first if enabled
    cache_key = f"hubspot_deal:{reservation_id}"
    if use_cache:
        cached_deal_id = cache.get(cache_key)
        if cached_deal_id:
            logger.debug(f"Cache hit for deal ID of reservation #{reservation_id}")
            return cached_deal_id

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
            deal_id = resp.results[0].id
            # Cache the result
            if use_cache:
                cache.set(cache_key, deal_id, CACHE_TIMEOUT["deal"])
            return deal_id
        return None
    except Exception as e:
        logger.error(f"Error searching for deal: {e}")
        return None


def invalidate_deal_cache(reservation_id):
    """Clear the cache for a specific deal"""
    cache_key = f"hubspot_deal:{reservation_id}"
    cache.delete(cache_key)


def get_reservation_payment_info(reservation):
    """
    Get payment information for a reservation,
    falling back to reservation.total_price if no payments exist
    """
    payment_status = "Pending"  # Default status
    payment_amount = float(reservation.total_price)  # Default amount
    payment_method = "N/A"  # Default method

    try:
        # Check if the reservation has payments
        if hasattr(reservation, "payments") and reservation.payments.exists():
            latest_payment = reservation.payments.last()

            if latest_payment:
                # Map payment status
                status_map = {
                    "pending": "Pending",
                    "card_saved": "Card On File",
                    "paid": "Paid",
                    "failed": "Failed",
                }
                payment_status = status_map.get(latest_payment.status, "Pending")

                # Use payment amount if available
                if (
                    hasattr(latest_payment, "amount")
                    and latest_payment.amount is not None
                ):
                    payment_amount = float(latest_payment.amount)

                # Get payment method if available
                if hasattr(latest_payment, "customer") and latest_payment.customer:
                    if (
                        hasattr(latest_payment.customer, "card_brand")
                        and latest_payment.customer.card_brand
                    ):
                        card_brand = latest_payment.customer.card_brand
                        card_last4 = latest_payment.customer.card_last4
                        if card_brand and card_last4:
                            payment_method = (
                                f"{card_brand.title()} ending in {card_last4}"
                            )

            logger.info(
                f"Found payment for reservation #{reservation.id}: status={payment_status}, amount={payment_amount}, method={payment_method}"
            )
        else:
            logger.info(
                f"No payments found for reservation #{reservation.id}, using total_price {payment_amount}"
            )
    except Exception as e:
        logger.warning(
            f"Error getting payment info for reservation #{reservation.id}: {e}"
        )

    return {
        "status": payment_status,
        "amount": payment_amount,
        "method": payment_method,
    }


@with_retries(max_attempts=3, retry_on=(Exception,))
def create_or_find_contact(reservation, use_cache=True):
    """Find or create a contact in HubSpot with improved caching and conflict handling"""
    if not HUBSPOT_AVAILABLE or not HUBSPOT_TOKEN:
        logger.info("HubSpot integration not available - skipping contact creation")
        return None

    customer = reservation.customer
    cache_key = f"hubspot_contact:{customer.email}"

    # Check cache first if enabled
    if use_cache:
        cached_contact_id = cache.get(cache_key)
        if cached_contact_id:
            logger.debug(f"Cache hit for contact ID of {customer.email}")
            return cached_contact_id

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
            contact_id = resp.results[0].id
            logger.info(f"Found contact {contact_id}")

            # Cache the result
            if use_cache:
                cache.set(cache_key, contact_id, CACHE_TIMEOUT["contact"])

            return contact_id

        # If no contact found, create a new one
        props = {
            "email": customer.email,
            "firstname": customer.first_name,
            "lastname": customer.last_name,
            "phone": customer.phone_number,
            "zip": customer.zipcode,
        }
        if reservation.travel_agent:
            props["travel_agent"] = reservation.travel_agent.agent_name

        new_ct = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=ContactInput(properties=props)
        )
        contact_id = new_ct.id
        logger.info(f"Created contact {contact_id}")

        # Cache the new contact ID
        if use_cache:
            cache.set(cache_key, contact_id, CACHE_TIMEOUT["contact"])

        return contact_id
    except Exception as e:
        # Check if the error is a conflict (contact already exists)
        if "Contact already exists" in str(e):
            # Extract the existing contact ID from the error message
            import re

            match = re.search(r"Existing ID: (\d+)", str(e))
            if match:
                contact_id = match.group(1)
                logger.info(f"Contact already exists, using ID: {contact_id}")

                # Cache the result
                if use_cache:
                    cache.set(cache_key, contact_id, CACHE_TIMEOUT["contact"])

                return contact_id

        logger.error(f"Error searching/creating contact: {e}")
        return None


@with_retries(max_attempts=3, retry_on=(Exception,))
def create_or_update_deal(reservation, contact_id):
    """Create or update a deal in HubSpot for the reservation with conflict handling"""
    if not HUBSPOT_AVAILABLE or not HUBSPOT_TOKEN:
        logger.info("HubSpot integration not available - skipping deal creation")
        return None

    # Check if deal already exists
    existing_deal_id = find_deal_by_reservation_id(reservation.id)
    is_update = existing_deal_id is not None

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
        reservation.vehicle.vehicle_type.title()
        if hasattr(reservation.vehicle, "vehicle")
        else "Vehicle"
    )

    # Format trip type for better display
    trip_type_display = reservation.trip_type.replace("_", " ").title()

    # Build description text
    description_text = (
        f"**Trip Details**\n"
        f"- Vehicle: {vehicle_type}\n"
        f"- Trip Type: {trip_type_display}\n"
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

    # Get payment info using the helper function
    payment_info = get_reservation_payment_info(reservation)

    # Deal properties
    deal_props = {
        "dealname": f"{reservation.status} | {reservation.vehicle} | {reservation.trip_type} | #{reservation.id} | {reservation.customer.get_full_name()}",
        "amount": payment_info["amount"],  # Use payment helper
        "pipeline": PIPELINE_ID,
        "dealstage": DEAL_STAGE_ID,
        "closedate": close_ms,
        "reservation_id": str(reservation.id),
        "reservation_status": reservation.status,
        "description": description_text,
        "payment_status": payment_info["status"],  # Use payment helper
        "payment_amount": payment_info["amount"],  # Use payment helper
        "payment_method": payment_info["method"],  # Use payment helper
    }

    # Add travel agent info if available
    if hasattr(reservation, "travel_agent") and reservation.travel_agent:
        try:
            deal_props["travel_agent"] = reservation.travel_agent.agent_name
        except:
            pass  # Skip travel agent info if there's an error

    try:
        if is_update:
            logger.info(f"Updating deal {existing_deal_id} with new properties")
            client.crm.deals.basic_api.update(
                deal_id=existing_deal_id,
                simple_public_object_input=DealInput(properties=deal_props),
            )
            deal_id = existing_deal_id
        else:
            logger.info(f"Creating deal For: {reservation.customer} - {reservation.id}")
            deal = client.crm.deals.basic_api.create(
                simple_public_object_input_for_create=DealInput(properties=deal_props)
            )
            deal_id = deal.id

            # Associate contact ↔ deal (only needed for new deals)
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

        # Invalidate cache for this deal
        invalidate_deal_cache(reservation.id)

        return deal_id
    except Exception as e:
        logger.error(f"Error creating/updating deal: {e}")
        return None


def sync_reservation_to_hubspot(reservation, force=False):
    """Main function to sync a reservation to HubSpot with better error handling"""
    if not HUBSPOT_AVAILABLE:
        logger.info("HubSpot integration not available - skipping HubSpot sync")
        return {"success": False, "error": "HubSpot integration not available"}

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

        # If we have a deal and we're not forcing an update, return early
        if existing_deal_id and not force:
            logger.info(
                f"Found existing deal {existing_deal_id} for reservation #{reservation.id}"
            )
            return {
                "success": True,
                "deal_id": existing_deal_id,
                "status": "existing",
            }

        # Create or find contact
        contact_id = create_or_find_contact(reservation)
        if not contact_id:
            logger.error(f"Failed to create or find contact for customer {cust.id}")
            return {"success": False, "error": "Failed to create or find contact"}

        logger.info(f"Using contact {contact_id} for customer {cust.get_full_name()}")

        # Create or update deal
        deal_id = create_or_update_deal(reservation, contact_id)
        if not deal_id:
            logger.error(
                f"Failed to create/update deal for reservation {reservation.id}"
            )
            return {"success": False, "error": "Failed to create/update deal"}

        action = "Updated" if existing_deal_id else "Created"
        logger.info(f"{action} deal {deal_id} for reservation #{reservation.id}")
        logger.info(
            f"✅ Successfully synced reservation #{reservation.id} → deal {deal_id}"
        )

        return {
            "success": True,
            "contact_id": contact_id,
            "deal_id": deal_id,
            "status": "updated" if existing_deal_id else "created",
        }
    except Exception as e:
        logger.error(
            f"Failed to sync reservation #{reservation.id} to HubSpot: {str(e)}"
        )
        return {"success": False, "error": str(e)}


@with_retries(max_attempts=3, retry_on=(Exception,))
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
    if not HUBSPOT_AVAILABLE:
        logger.info(
            "HubSpot integration not available - skipping payment status update"
        )
        return {"success": False, "error": "HubSpot integration not available"}

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
            props["amount"] = (
                float(payment_amount) if payment_amount is not None else None
            )

        # Update the deal
        client.crm.deals.basic_api.update(
            deal_id=deal_id, simple_public_object_input=DealInput(properties=props)
        )

        # Invalidate cache for this deal
        invalidate_deal_cache(reservation_id)

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
    if not HUBSPOT_AVAILABLE:
        logger.info("HubSpot integration not available - skipping payment update")
        return {"success": False, "error": "HubSpot integration not available"}

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
        props["payment_status"] = kwargs["status"]

    if "amount" in kwargs and kwargs["amount"] is not None:
        props["payment_amount"] = float(kwargs["amount"])

    if "method" in kwargs and kwargs["method"]:
        props["payment_method"] = kwargs["method"]

    # Only proceed if we have properties to update
    if not props:
        logger.warning("No payment properties to update")
        return {"success": False, "error": "No properties to update"}

    try:
        # Update the deal
        client.crm.deals.basic_api.update(
            deal_id=deal_id, simple_public_object_input=DealInput(properties=props)
        )

        # Invalidate cache for this deal
        invalidate_deal_cache(reservation_id)

        logger.info(f"Updated payment information for deal {deal_id}")
        return {"success": True, "deal_id": deal_id}
    except Exception as e:
        logger.error(f"Error updating payment information: {e}")
        return {"success": False, "error": str(e)}
