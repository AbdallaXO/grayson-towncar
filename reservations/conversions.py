import requests
import hashlib
import time
import os
import logging
import uuid

# Configure logging
logging.basicConfig(level=logging.INFO)
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


# Shared function to send event
def send_capi_event(event_name, user_data, custom_data=None, request=None, event_id=None):
    if not FB_PIXEL_ID or not FB_CAPI_ACCESS_TOKEN:
        logger.error(
            "Missing required environment variables: FB_PIXEL_ID or FB_CAPI_ACCESS_TOKEN"
        )
        return None

    url = f"https://graph.facebook.com/v19.0/{FB_PIXEL_ID}/events"
    event_time = int(time.time())

    event_payload = {
        "event_name": event_name,
        "event_time": event_time,
        "action_source": "website",
        "event_source_url": request.build_absolute_uri()
        if request
        else "https://www.graysontowncar.com",
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
            url, params={"access_token": FB_CAPI_ACCESS_TOKEN}, json=payload
        )
        response.raise_for_status()
        response_data = response.json()
        logger.info(f"Successfully sent {event_name} event to Meta CAPI. Response: {response_data}")
        return response_data
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending {event_name} event to Meta CAPI: {str(e)}")
        return None


# Lead event (quote form)
def send_lead_event(lead, request):
    user_data = {
        "em": hash_data(lead.email),
        "ph": hash_data(lead.phone),
        "fn": hash_data(lead.first_name),
        "ln": hash_data(lead.last_name),
        "client_ip_address": request.META.get("REMOTE_ADDR"),
    }
    return send_capi_event("Lead", user_data, request=request)


# InitiateCheckout event (reservation submission)
def send_initiate_checkout_event(reservation, request):
    user_data = {
        "em": hash_data(reservation.customer.email),
        "ph": hash_data(reservation.customer.phone_number),
        "fn": hash_data(reservation.customer.first_name),
        "ln": hash_data(reservation.customer.last_name),
        "zp": hash_data(reservation.customer.zipcode),
        "client_user_agent": request.headers.get("User-Agent"),
        "client_ip_address": request.META.get("REMOTE_ADDR"),
    }
    return send_capi_event("InitiateCheckout", user_data, request=request)


# Purchase event (payment successful via Stripe webhook)
def send_purchase_event(reservation, value=None, event_id=None, request=None):
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
    
    # Add IP and User-Agent if request is available
    if request:
        user_data["client_ip_address"] = request.META.get("REMOTE_ADDR")
        user_data["client_user_agent"] = request.headers.get("User-Agent")
        
        # Extract Facebook cookies for better attribution (_fbp and _fbc)
        cookies = request.COOKIES
        if "_fbp" in cookies:
            user_data["fbp"] = cookies["_fbp"]
        if "_fbc" in cookies:
            user_data["fbc"] = cookies["_fbc"]
    else:
        user_data["client_ip_address"] = None

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

    return send_capi_event("Purchase", user_data, custom_data=custom_data, request=request, event_id=event_id)
