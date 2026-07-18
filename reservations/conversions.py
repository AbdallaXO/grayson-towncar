import requests
import hashlib
import time
import os
import logging
import uuid

logger = logging.getLogger(__name__)

# Load from Railway environment variables
FB_PIXEL_ID = os.getenv("FB_PIXEL_ID")
FB_CAPI_ACCESS_TOKEN = os.getenv("FB_CAPI_ACCESS_TOKEN")
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "USD")


# Hash helper
def hash_data(data):
    if not data:
        return None
    return hashlib.sha256(data.strip().lower().encode("utf-8")).hexdigest()


def extract_request_meta(request):
    """Snapshot the request-derived CAPI signals into plain primitives.

    A live request object is not thread-safe and is torn down after the response,
    so callers that want to fire an event in a background thread must snapshot
    these here (on the request thread) and pass the dict as ``meta=`` instead of
    handing the request to the thread.
    """
    if request is None:
        return {}
    cookies = getattr(request, "COOKIES", {}) or {}
    return {
        "client_user_agent": request.headers.get("User-Agent"),
        "client_ip_address": request.META.get("REMOTE_ADDR"),
        "fbp": cookies.get("_fbp"),
        "fbc": cookies.get("_fbc"),
        "event_source_url": request.build_absolute_uri(),
    }


# Shared function to send event
def send_capi_event(event_name, user_data, custom_data=None, request=None, event_id=None, meta=None):
    if not FB_PIXEL_ID or not FB_CAPI_ACCESS_TOKEN:
        logger.error(
            "Missing required environment variables: FB_PIXEL_ID or FB_CAPI_ACCESS_TOKEN"
        )
        return None

    url = f"https://graph.facebook.com/v19.0/{FB_PIXEL_ID}/events"
    event_time = int(time.time())

    # Prefer a pre-extracted meta snapshot (background-safe) over the live request.
    if meta and meta.get("event_source_url"):
        event_source_url = meta["event_source_url"]
    elif request is not None:
        event_source_url = request.build_absolute_uri()
    else:
        event_source_url = "https://www.graysontowncar.com"

    event_payload = {
        "event_name": event_name,
        "event_time": event_time,
        "action_source": "website",
        "event_source_url": event_source_url,
        "user_data": user_data,
    }

    # Add event_id for deduplication (required for Purchase events)
    if event_id:
        event_payload["event_id"] = event_id
    else:
        # Generate a unique event_id if not provided
        event_payload["event_id"] = str(uuid.uuid4())

    if custom_data:
        event_payload["custom_data"] = custom_data

    payload = {"data": [event_payload]}

    try:
        response = requests.post(
            url, params={"access_token": FB_CAPI_ACCESS_TOKEN}, json=payload,
            timeout=5
        )
        response.raise_for_status()
        response_data = response.json()
        logger.info(f"Successfully sent {event_name} event to Meta CAPI. Response: {response_data}")
        return response_data
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending {event_name} event to Meta CAPI: {str(e)}")
        return None


def _augment_user_data(user_data, request=None, meta=None):
    """Add IP, User-Agent and Meta browser cookies (_fbp / _fbc) to user_data.

    These raw (un-hashed) signals are the strongest match keys Meta has, so
    every event type should send them whenever they are available. Prefer a
    pre-extracted ``meta`` snapshot (background-safe); otherwise read the live
    request. Keys with no value are dropped so we never POST nulls (which lower
    match quality). Mutates and returns user_data.
    """
    if meta is None:
        if request is None:
            return {k: v for k, v in user_data.items() if v}
        meta = extract_request_meta(request)

    if meta.get("client_user_agent"):
        user_data["client_user_agent"] = meta["client_user_agent"]
    if meta.get("client_ip_address"):
        user_data["client_ip_address"] = meta["client_ip_address"]
    if meta.get("fbp"):
        user_data["fbp"] = meta["fbp"]
    if meta.get("fbc"):
        user_data["fbc"] = meta["fbc"]

    return {k: v for k, v in user_data.items() if v}


# Lead event (quote form)
def send_lead_event(lead, request, event_id=None):
    user_data = {
        "em": hash_data(lead.email),
        "ph": hash_data(lead.phone),
        "fn": hash_data(lead.first_name),
        "ln": hash_data(lead.last_name),
        "external_id": str(lead.id) if getattr(lead, "id", None) else None,
    }
    user_data = _augment_user_data(user_data, request)
    return send_capi_event("Lead", user_data, request=request, event_id=event_id)


# InitiateCheckout event (reservation submission)
def send_initiate_checkout_event(reservation, request=None, event_id=None, meta=None):
    """Fire an InitiateCheckout event. Pass ``meta`` (from ``extract_request_meta``)
    instead of ``request`` when calling this from a background thread."""
    user_data = {
        "em": hash_data(reservation.customer.email),
        "ph": hash_data(reservation.customer.phone_number),
        "fn": hash_data(reservation.customer.first_name),
        "ln": hash_data(reservation.customer.last_name),
        "zp": hash_data(reservation.customer.zipcode),
        "external_id": str(reservation.id) if getattr(reservation, "id", None) else None,
    }
    user_data = _augment_user_data(user_data, request=request, meta=meta)
    return send_capi_event("InitiateCheckout", user_data, request=request, event_id=event_id, meta=meta)


# Purchase event (payment successful via Stripe webhook)
def send_purchase_event(reservation, value=None, event_id=None, request=None, meta=None):
    """
    Send Purchase event to Meta Conversions API.
    
    Args:
        reservation: Reservation object
        value: Optional payment amount. If not provided, uses reservation.total_price (matches Google Analytics)
        event_id: Optional event_id for deduplication. If not provided, generates one.
        request: Optional request object for IP/UA tracking
    """
    # Use reservation.total_price if value not provided (matches Google Analytics behavior)
    if value is None:
        value = float(reservation.total_price) if reservation.total_price else 0.0
    
    user_data = {
        "em": hash_data(reservation.customer.email),
        "ph": hash_data(reservation.customer.phone_number),
        "fn": hash_data(reservation.customer.first_name),
        "ln": hash_data(reservation.customer.last_name),
        "zp": hash_data(reservation.customer.zipcode),
        "external_id": str(reservation.id),
    }
    # IP, User-Agent and _fbp/_fbc cookies (best attribution signals)
    user_data = _augment_user_data(user_data, request=request, meta=meta)

    custom_data = {
        "currency": "USD", 
        "value": value,
        "content_type": "product",
        "content_ids": [str(reservation.id)],
    }
    
    # Add transaction ID if available
    if reservation.payments.exists():
        latest_payment = reservation.payments.latest("created_at")
        if latest_payment.stripe_payment_intent_id:
            custom_data["order_id"] = latest_payment.stripe_payment_intent_id

    return send_capi_event("Purchase", user_data, custom_data=custom_data, request=request, event_id=event_id, meta=meta)
