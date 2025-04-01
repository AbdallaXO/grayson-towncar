from django import forms
from .models import Reservation, Customer, Leg, Flight
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Submit


class CustomerForm(forms.ModelForm):
    """A Form that contians all the customers informations such as
    first_name last_name etc..."""

    class Meta:
        model = Customer
        fields = '__all__'
        exclude = ['is_returning', 'reservation_count']
        labels = {
            'first_name':'First Name',
            'last_name':'Last Name'
        }


class ReservationForm(forms.ModelForm):
    """A Form that contains everything related to a reservation such as rates/routes/trip_types"""
    class Meta:
        model = Reservation
        exclude = ('base_price', 'payment_status', 'stripe_payment_id', 'status', 'additional_charges')
        labels = {'passenger_count':'Number Of Passengers', 'has_children':'Children', 'carseat_type':'Carseat Type', 'luggage_count':'Luggage Count',
                  'carseat_type':'Carseat Choices'}#! what if need more than 1 carseat? TODO
        help_texts = {
            'has_children':'Check this if you have any children that need carseats/boosters',
            'store_stop':'Check This if you Would like to Add a Grocery Stop Please Note Store Stops Are Only From MCO',
        }
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields['special_requests'].widget.attrs.update({'placeholder':'Any Special Requests E.G store stop, surprise for someone'})


class FlightForm(forms.ModelForm):
    class Meta:
        model = Flight
        fields = '__all__'        
class LegForm(forms.ModelForm):
    class Meta:
        model = Leg
        fields = '__all__'


