from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

def send_reservation_confirmation(reservation):
    """This Reservation is Called in the Signal
    When a Reservation is created with the reservation Object
    Renders a nicely formatted HTML and emails a Confirmation"""
    
    context = {
        'name':reservation.customer.first_name + reservation.customer.last_name,
        'reservation_id':reservation.id,
        'type':reservation.trip_type,
        'date':reservation.created_at,
        
    }
    
    subject = "Your Grayson Towncar Reservation is Confirmed"
    from_email = 'info@graysontowncar.com'
    to = [reservation.customer.email]
    
    text_content = render_to_string('reservations/confirmation.txt', context)
    html_content = render_to_string('reservations/confirmation.txt', context)
    
    msg = EmailMultiAlternatives(subject, text_content, from_email, to)
    msg.attach_alternative(html_content, "text/html")
    msg.send()