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
    pickup_location = forms.CharField(required=True)
    dropoff_location = forms.CharField(required=True)
    
    class Meta:
        model = Reservation
        fields = ['pickup_location', 'dropoff_location', 'pickup_date', 'pickup_time', 'luggage_count', 'pasenger_count', 'special_requests']
        widgets = {
            'pickup_date': forms.DateInput(attrs={'type':'date', 'class':'form-control rounded-3 shadow-sm'}),
            'pickup_time': forms.TimeInput(attrs={'type':'time', 'class':'form-control rounded-3 shadow-sm'})
        }

    def __init__(self, *args, **kwargs):
        super(ReservationForm, self).__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs.update({'class':'form-control'})
        self.fields['special_requests'].widget.attrs.update({'placeholder':'Any Special Requests e.g carseats, booster seats, grocery store surprise etc.',})
        self.fields['pickup_location'].widget.attrs.update({'id': 'pickup-location', 'placeholder': 'Enter pickup address'})
        self.fields['dropoff_location'].widget.attrs.update({'id': 'dropoff-location', 'placeholder': 'Enter dropoff address'})
    
  