from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
import logging
logger = logging.getLogger(__name__)



def send_reservation_confirmation(reservation):
    """This Reservation is Called in the View
    When a Reservation is created with the reservation Object
    Renders a nicely formatted HTML and emails a Confirmation"""
    try:
        context = {
            "reservation": reservation,
            "legs": reservation.legs.all(),
        }
        subject = f"Hello {reservation.customer.first_name}, Your Grayson Towncar Reservation is Confirmed"
        from_email = "reservations@graysontowncar.com"
        to = [reservation.customer.email]

        # Skip the .txt file for now
        html_content = render_to_string("users/confirmation_email.html", context)

        msg = EmailMultiAlternatives(subject, "", from_email, to)
        msg.attach_alternative(html_content, "text/html")
        msg.send()
    except Exception as e:
        logger.error(f"Error sending confirmation email: {e}")




def thankyou_email(user):
    """This Reservation is Called in the View
    When a Reservation is created with the reservation Object
    Renders a nicely formatted HTML and emails a Confirmation"""
    try:
        context = {'name': user.name.title(),}
        subject = f"Hello {user.name.title() if user.name else user.first_name.title()}, We've Recieved Your Message."
        from_email = "info@graysontowncar.com"
        logger.info(f'Sending Email to ... {user.email}')
        to = [user.email]
        html_content = render_to_string("users/user_contact_email.html", context)

        msg = EmailMultiAlternatives(subject, "", from_email, to)
        msg.attach_alternative(html_content, "text/html")
        msg.send()
    except Exception as e:
        logger.error(f"Error sending confirmation email: {e}")



