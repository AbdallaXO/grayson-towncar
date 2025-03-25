from django import forms
from .models import Reservation, Customer

class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = '__all__'
    def __init__(self, *args, **kwargs):
        super(CustomerForm, self).__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs.update({'class':'form-control'})
        


class ReservationForm(forms.ModelForm):
    class Meta:
        model = Reservation
        fields = ['pickup_date', 'pickup_time', 'luggage_count', 'pasenger_count', 'special_requests']
    def __init__(self, *args, **kwargs):
        super(ReservationForm, self).__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs.update({'class':'form-control'})
        self.fields['special_requests'].widget.attrs.update({'placeholder':'Any Special Requests e.g carseats, booster seats, grocery store surprise etc.',})
    
  