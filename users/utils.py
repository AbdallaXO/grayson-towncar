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
            logger.error(
                f"All {max_attempts} attempts failed for {func.__name__}: {last_error}"
            )
            raise last_error

        return wrapper

    return decorator




@with_retries(max_attempts=3, retry_on=(Exception,))
def create_or_find_travel_agent(travel_agent, use_cache=True):
    """Creates a Travel AGENT OBJECT IN HUBSPOT OR GETS IT IF IT EXISTS"""
    if not HUBSPOT_AVAILABLE or not HUBSPOT_TOKEN:
        logger.info("HubSpot integration not available - skipping contact creation")
        return None

    agent = travel_agent
    cache_key = f"hubspot_contact:{travel_agent.email}"

    # Check cache first if enabled
    if use_cache:
        cached_contact_id = cache.get(cache_key)
        if cached_contact_id:
            logger.debug(f"Cache hit for contact ID of {travel_agent.email}")
            return cached_contact_id

    # Search by email
    body = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": travel_agent.email}
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

        try:
            first, last = agent.agent_name.strip().split(' ')[0], agent.agent_name.strip().split(' ')[0]
        except Exception as e:
            first = agent.agent_name
            last = ''
        props = {
            "email": agent.email,
            "firstname": first,
            "lastname": last,
            "phone": agent.phone_number,
            "travel_agent": agent.agency_name,
        }

        new_ct = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=ContactInput(properties=props)
        )
        contact_id = new_ct.id
        logger.info(f"Created contact {contact_id}")
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