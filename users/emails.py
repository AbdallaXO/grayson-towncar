from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
import logging
from django.utils import timezone

logger = logging.getLogger(__name__)


def send_reservation_confirmation(reservation):
    """This Reservation is Called in the View
    When a Reservation is created with the reservation Object
    Renders a nicely formatted HTML and emails a Confirmation"""
    logger.info(f"Preparing to send reservation confirmation for {reservation.customer}")

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
        from_email = "info@graysontowncar.com"
        logger.info(f"Sending Email to ... {instance.email}")
        to = [instance.email]
        html_content = render_to_string("users/partner_contact_email.html")

        msg = EmailMultiAlternatives(subject, "", from_email, to)
        msg.attach_alternative(html_content, "text/html")
        msg.send()
    except Exception as e:
        logger.error(f"Error sending confirmation email: {e}")


def send_internal_confirmation(reservation):
    """Emails Self when a reservation gets made in case of any errors and customer does not get an email"""
    logger.info(f"Preparing to send reservation confirmation for {reservation.customer}")

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
