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

logger = logging.getLogger(__name__)


@login_required
@require_POST
def send_reservation_confirmation_ajax(request):
    """
    AJAX endpoint to send reservation confirmation email.
    Uses the existing send_reservation_confirmation function.
    """
    if not request.user.is_superuser:
        return JsonResponse(
            {"success": False, "error": "Permission denied"}, status=403
        )

    try:
        data = json.loads(request.body)
        reservation_id = data.get("reservation_id")

        if not reservation_id:
            return JsonResponse(
                {"success": False, "error": "Missing reservation ID"}, status=400
            )

        # Get the reservation
        reservation = get_object_or_404(Reservation, uuid=reservation_id)

        # Use the existing function to send the email
        send_reservation_confirmation(reservation)

        # Log the action in private notes
        timestamp = timezone.now().strftime("%Y-%m-%d %H:%M")
        note_addition = (
            f"\n[{timestamp}] Confirmation email sent by {request.user.username}"
        )

        if reservation.private_notes:
            reservation.private_notes += note_addition
        else:
            reservation.private_notes = note_addition

        reservation.save(update_fields=["private_notes"])

        return JsonResponse({"success": True})

    except Exception as e:
        logger.error(f"Error sending confirmation email via AJAX: {str(e)}")
        return JsonResponse({"success": False, "error": str(e)})


def send_reservation_confirmation(reservation):
    """This Reservation is Called in the View
    When a Reservation is created with the reservation Object
    Renders a nicely formatted HTML and emails a Confirmation"""
    logger.info(
        f"Preparing to send reservation confirmation for {reservation.customer}"
    )

    try:
        context = {
            "reservation": reservation,
            "legs": reservation.legs.all(),
            "date": timezone.now().date(),
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


def thankyou_email(instance):
    """This Reservation is Called in the View
    When a Reservation is created with the reservation Object
    Renders a nicely formatted HTML and emails a Confirmation"""
    try:
        name = instance.first_name if instance.first_name else instance.name
        subject = f"Hello {name}, We've Recieved Your Message."
        from_email = "contact@graysontowncar.com"
        logger.info(f"Sending Email to ... {instance.email}")
        to = [instance.email, "contact@graysontowncar.com"]
        html_content = render_to_string("users/partner_contact_email.html")

        msg = EmailMultiAlternatives(subject, "", from_email, to)
        msg.attach_alternative(html_content, "text/html")
        msg.send()
    except Exception as e:
        logger.error(f"Error sending confirmation email: {e}")


def send_internal_confirmation(reservation):
    """Emails Self when a reservation gets made in case of any errors and customer does not get an email"""
    logger.info(
        f"Preparing to send reservation confirmation for {reservation.customer}"
    )

    try:
        context = {
            "reservation": reservation,
            "legs": reservation.legs.all(),
            "date": timezone.now().date(),
        }

        subject = "Reservation Submission"
        from_email = "reservations@graysontowncar.com"
        to = ["reservations@graysontowncar.com"]
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


def agent_register_email(instance):
    """When a travelAgent Succesfully Registers this is a Thank You Email
    And Some Instructions Sent Along with it"""
    try:
        name = instance.agent_name.split(" ")[0]
        subject = f"Welcome to Grayson Towncar! Your Agent Account is Now Live!"
        from_email = "contact@graysontowncar.com"
        logger.info(f"Sending Email to ... {instance.user.email}")
        to = [instance.user.email, "contact@graysontowncar.com"]
        html_content = render_to_string(
            "users/agent_register_email.html", {"name": name}
        )

        msg = EmailMultiAlternatives(subject, "", from_email, to)
        msg.attach_alternative(html_content, "text/html")
        msg.send()
    except Exception as e:
        logger.error(f"Error sending confirmation email: {e}")
