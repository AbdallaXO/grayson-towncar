from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
import logging
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from reservations.models import Reservation
from django.shortcuts import get_object_or_404
from django.utils import timezone
import logging
from django.http import JsonResponse
from django.views.decorators.http import require_POST
import json
import threading
import time

logger = logging.getLogger(__name__)


def _send_email_with_retry(email_func, max_retries=3):
    """Send email with retry logic in background thread"""
    def background_send():
        for attempt in range(max_retries):
            try:
                email_func()
                logger.info(f"Email sent successfully on attempt {attempt + 1}")
                return  # Success, exit retry loop
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    logger.warning(f"Email attempt {attempt + 1} failed, retrying in {wait_time}s: {e}")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Email failed after {max_retries} attempts: {e}")
    
    thread = threading.Thread(target=background_send)
    thread.daemon = True
    thread.start()


@login_required
@require_POST
def send_reservation_confirmation_ajax(request):
    """
    AJAX endpoint to send reservation confirmation email
    """
    try:
        data = json.loads(request.body)
        reservation_id = data.get("reservation_id")
        reservation = get_object_or_404(Reservation, uuid=reservation_id)
        
        # Check permissions - staff or superuser
        if not request.user.is_staff:
            return JsonResponse({"success": False, "error": "Permission denied"})
        
        send_reservation_confirmation(reservation)
        
        return JsonResponse({"success": True})
    
    except Exception as e:
        logger.error(f"Error sending confirmation email: {e}")
        return JsonResponse({"success": False, "error": str(e)})


def send_reservation_confirmation(reservation):
    """This Reservation is Called in the View
    When a Reservation is created with the reservation Object
    Renders a nicely formatted HTML and emails a Confirmation"""
    logger.info(
        f"Preparing to send reservation confirmation for {reservation.customer}"
    )

    def _send_email():
        try:
            legs = reservation.legs.all()
            # Check if any leg is a return trip
            has_return_trip = any(leg.get_trip_type() == 'return' for leg in legs)
            
            context = {
                "reservation": reservation,
                "legs": legs,
                "date": timezone.now().date(),
                "has_return_trip": has_return_trip,
            }

            subject = "Thank you for booking with Grayson Towncar!"
            from_email = "reservations@graysontowncar.com"
            to = [reservation.customer.email]
            logger.info(f"Email subject: {subject}")
            logger.info(f"Sending to: {to}")
            html_content = render_to_string("users/confirmation_email.html", context)
            logger.info("HTML content rendered successfully")

            msg = EmailMultiAlternatives(subject, "", from_email, to)
            msg.attach_alternative(html_content, "text/html")
            msg.send()

            logger.info(
                f"Confirmation email sent successfully for reservation {reservation.uuid}"
            )

        except Exception as e:
            logger.exception(
                f"Error sending confirmation email for reservation {reservation.uuid}: {e}"
            )
            raise  # Re-raise for retry logic

    # Send with retry in background thread
    _send_email_with_retry(_send_email, max_retries=3)


def send_refund_request_notification(reservation):
    """
    Send email notification to admin when a refund is requested.
    """
    logger.info(f"Preparing to send refund request notification for reservation {reservation.id}")

    def _send_email():
        try:
            context = {
                "reservation": reservation,
                "requested_by": reservation.refund_requested_by.get_full_name() if reservation.refund_requested_by else "Unknown",
                "requested_at": reservation.refund_requested_at,
                "refund_reason": reservation.refund_reason,
                "refund_amount": reservation.refund_amount,
                "total_paid": reservation.total_paid,
                "admin_url": f"https://www.graysontowncar.com/dispatching/refund-management/",
            }

            subject = f"Refund Requested - Reservation #{reservation.id}"
            from_email = "reservations@graysontowncar.com"
            to = ["admin@graysontowncar.com"]
            
            html_content = render_to_string("users/refund_request_email.html", context)
            
            msg = EmailMultiAlternatives(subject, "", from_email, to)
            msg.attach_alternative(html_content, "text/html")
            msg.send()

            logger.info(f"Refund request notification sent successfully for reservation {reservation.id}")

        except Exception as e:
            logger.exception(f"Error sending refund request notification: {e}")
            raise

    # Send with retry in background thread
    _send_email_with_retry(_send_email, max_retries=3)


def agent_register_email(instance):
    """
    Sends a welcome email to new travel agents after registration.
    """
    try:
        context = {
            "agent": instance,
            "agent_name": instance.agent_name or instance.user.get_full_name() or instance.user.username,
            "email": instance.user.email,
        }
        subject = "Welcome to Grayson Towncar Travel Agent Portal!"
        from_email = "reservations@graysontowncar.com"
        to = [instance.user.email]
        html_content = render_to_string("users/agent_register_email.html", context)

        msg = EmailMultiAlternatives(subject, "", from_email, to)
        msg.attach_alternative(html_content, "text/html")
        msg.send()
        logger.info(f"Welcome email sent to {instance.user.email}")

    except Exception as e:
        logger.error(f"Error sending agent welcome email: {e}")


def thankyou_email(instance):
    """
    Sends a thank you email to the customer after they submit a contact form.
    """
    try:
        context = {"name": instance.name, "email": instance.email}
        subject = "Thank you for contacting Grayson Towncar!"
        from_email = "reservations@graysontowncar.com"
        to = [instance.email]
        html_content = render_to_string("users/thankyou_email.html", context)

        msg = EmailMultiAlternatives(subject, "", from_email, to)
        msg.attach_alternative(html_content, "text/html")
        msg.send()
        logger.info(f"Thank you email sent to {instance.email}")

    except Exception as e:
        logger.error(f"Error sending thank you email: {e}")
