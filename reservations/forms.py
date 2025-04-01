from django import forms
from .models import Reservation, Customer


class CustomerForm(forms.ModelForm):
    """A Form that contians all the customers informations such as
    first_name last_name etc..."""
    class Meta:
        model = Customer
        fields = "__all__"


class ReservationForm(forms.ModelForm):
    """A Form that contains everything related to a reservation such as rates/routes/trip_types"""
    class Meta:
        model = Reservation
        fields = '__all__'
        
    
# def __init__(self, *args, **kwargs):
#     super().__init__(*args, **kwargs)
#     for visible in self.visible_fields():
#         visible.field.widget.attrs['class'] = 'my-custom-class'