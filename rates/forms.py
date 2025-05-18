from django import forms
from django.core.validators import RegexValidator
from .models import Lead, Vehicle, Location, Route

class LeadForm(forms.ModelForm):
    """
    Enhanced lead form with proper field relationships and validation.
    """
    # Custom field definitions with better validation
    phone = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={
            'class': 'form-control form-control-lg',
            'placeholder': 'Phone Number'
        })
    )
    
    # Hidden fields for calculated values and form selections
    vehicle = forms.ModelChoiceField(
        queryset=Vehicle.objects.all(),
        widget=forms.HiddenInput(),
        required=True
    )
    
    origin = forms.ModelChoiceField(
        queryset=Location.objects.all(),
        widget=forms.HiddenInput(),
        required=True
    )
    
    destination = forms.ModelChoiceField(
        queryset=Location.objects.all(),
        widget=forms.HiddenInput(),
        required=True
    )
    
    trip_type = forms.ChoiceField(
        choices=Lead.TRIP_TYPES,
        widget=forms.HiddenInput(),
        required=True
    )
    
    quoted_price = forms.DecimalField(
        required=False, 
        decimal_places=2, 
        max_digits=10, 
        widget=forms.HiddenInput()
    )
    
    class Meta:
        model = Lead
        fields = [
            'first_name', 'last_name', 'email', 'phone', 
            'vehicle', 'origin', 'destination', 'trip_type', 'quoted_price'
        ]
        widgets = {
            'first_name': forms.TextInput(attrs={
                'class': 'form-control form-control-lg',
                'placeholder': 'First Name'
            }),
            'last_name': forms.TextInput(attrs={
                'class': 'form-control form-control-lg',
                'placeholder': 'Last Name'
            }),
            'email': forms.EmailInput(attrs={
                'class': 'form-control form-control-lg',
                'placeholder': 'Email Address'
            }),
        }
    
    def clean(self):
        """
        Custom validation to ensure valid selections.
        """
        cleaned_data = super().clean()
        origin = cleaned_data.get('origin')
        destination = cleaned_data.get('destination')
        
        if origin and destination and origin == destination:
            raise forms.ValidationError("Origin and destination cannot be the same.")
        
        return cleaned_data