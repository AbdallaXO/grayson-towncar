from django import forms
from .models import Reservation, Customer
class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = '__all__'
class ReservationForm(forms.ModelForm):
    class Meta:
        model = Reservation
        fields = '__all__'

class CustomerReservationForm(forms.ModelForm):
    class Meta:
        ...
    
# def __init__(self, *args, **kwargs):
#     super().__init__(*args, **kwargs)
#     for visible in self.visible_fields():
#         visible.field.widget.attrs['class'] = 'my-custom-class'