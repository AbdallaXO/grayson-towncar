from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string


def send_reservation_confirmation(reservation):
    """This Reservation is Called in the Signal
    When a Reservation is created with the reservation Object
    Renders a nicely formatted HTML and emails a Confirmation"""
    try:
        context = {
            "first_name": reservation.customer.first_name,
            "full_name": reservation.customer.get_full_name(),
            "reservation_id": reservation.id,
            "legs": reservation.legs.all(),
            "type": reservation.trip_type,
            "date": reservation.created_at,
            "reservation": reservation,
        }
        subject = f"Hello {reservation.customer.first_name} Your Grayson Towncar Reservation is Confirmed"
        from_email = "reservations@graysontowncar.com"
        to = [reservation.customer.email]

        text_content = render_to_string("reservations/confirmation.txt", context)
        html_content = render_to_string("reservations/confirmation.html", context)

        msg = EmailMultiAlternatives(subject, text_content, from_email, to)
        msg.attach_alternative(html_content, "text/html")
        msg.send()
    except Exception as e:
        pass
