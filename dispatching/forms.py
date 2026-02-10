# dispatching/forms.py

from django import forms
from django.utils import timezone
from decimal import Decimal
from reservations.models import Customer, Reservation, Leg, Flight
from rates.models import Vehicle, Rate


class DispatcherCustomerForm(forms.ModelForm):
    """Dispatcher form for customer information with enhanced styling"""

    class Meta:
        model = Customer
        fields = ["first_name", "last_name", "email", "phone_number", "zipcode"]
        widgets = {
            "first_name": forms.TextInput(
                attrs={
                    "class": "form-control form-control-lg",
                    "placeholder": "First Name",
                    "autocomplete": "given-name",
                    "autofocus": True,
                    "required": True,
                }
            ),
            "last_name": forms.TextInput(
                attrs={
                    "class": "form-control form-control-lg",
                    "placeholder": "Last Name",
                    "autocomplete": "family-name",
                    "required": True,
                }
            ),
            "email": forms.EmailInput(
                attrs={
                    "class": "form-control form-control-lg",
                    "placeholder": "customer@example.com",
                    "type": "email",
                    "autocomplete": "email",
                    "required": True,
                }
            ),
            "phone_number": forms.TextInput(
                attrs={
                    "class": "form-control form-control-lg",
                    "placeholder": "(555) 123-4567",
                    "required": True,
                    "type": "tel",
                    "autocomplete": "tel",
                }
            ),
            "zipcode": forms.TextInput(
                attrs={
                    "class": "form-control form-control-lg", 
                    "placeholder": "32819",
                    "autocomplete": "postal-code"
                }
            ),
        }
        labels = {
            "first_name": "Customer First Name",
            "last_name": "Customer Last Name",
            "email": "Email Address",
            "phone_number": "Phone Number",
            "zipcode": "Zip Code",
        }

    def save(self, commit=True):
        """Override save to handle existing customers"""
        if commit:
            # Check for existing customer
            existing_customer = Customer.objects.filter(
                email=self.cleaned_data['email'],
                phone_number=self.cleaned_data['phone_number']
            ).first()
            
            if existing_customer:
                # Update existing customer with any new info
                for field in self.cleaned_data:
                    if self.cleaned_data[field]:  # Only update if field has value
                        setattr(existing_customer, field, self.cleaned_data[field])
                existing_customer.is_returning = True
                existing_customer.reservation_count += 1
                existing_customer.save()
                return existing_customer
            else:
                # Create new customer
                customer = super().save(commit=False)
                customer.reservation_count = 1
                if commit:
                    customer.save()
                return customer
        return super().save(commit)


class DispatcherReservationForm(forms.ModelForm):
    """Dispatcher form for reservation details with manual pricing"""
    
    # Vehicle selection field
    
    manual_vehicle = forms.ModelChoiceField(
        queryset=Vehicle.objects.all(),
        required=True,
        widget=forms.Select(
            attrs={
                "class": "form-control form-control-lg",
            }
        ),
        label="Vehicle Type",
        help_text="Select the vehicle for this reservation"
    )

    class Meta:
        model = Reservation
        fields = [
            # Passenger details first
            "passenger_count",
            "luggage_count",
            "need_carseats",
            "rf_carseats",
            "ff_carseats",
            "booster_seats",
            "store_stop",
            "special_requests",
            # Vehicle selection only - pricing moved to separate step
            "manual_vehicle",
        ]
        widgets = {
            "passenger_count": forms.NumberInput(
                attrs={
                    "class": "form-control form-control-lg",
                    "min": 1,
                    "max": 14,
                    "type": "number",
                    "value": 1,
                }
            ),
            "luggage_count": forms.NumberInput(
                attrs={
                    "class": "form-control form-control-lg",
                    "min": 0,
                    "max": 12,
                    "type": "number",
                    "value": 1,
                }
            ),
            "store_stop": forms.CheckboxInput(
                attrs={"class": "form-check-input form-check-input-lg"}
            ),
            "special_requests": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": "Any special requests, car seat needs, or notes...",
                }
            ),
            "need_carseats": forms.CheckboxInput(
                attrs={
                    "class": "form-check-input form-check-input-lg",
                    "id": "id_need_carseats",
                }
            ),
            "rf_carseats": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "type": "number",
                    "min": 0,
                    # No max limit for dispatchers
                }
            ),
            "ff_carseats": forms.NumberInput(
                attrs={
                    "class": "form-control", 
                    "type": "number",
                    "min": 0,
                    # No max limit for dispatchers
                }
            ),
            "booster_seats": forms.NumberInput(
                attrs={
                    "class": "form-control", 
                    "type": "number",
                    "min": 0,
                    # No max limit for dispatchers
                }
            ),

        }
        labels = {
            "passenger_count": "Number of Passengers",
            "luggage_count": "Number of Bags",
            "store_stop": "Grocery Store Stop",
            "special_requests": "Special Requests",
            "need_carseats": "Car Seats Needed?",
            "rf_carseats": "Rear-Facing Seats",
            "ff_carseats": "Forward-Facing Seats",
            "booster_seats": "Booster Seats",
        }
        help_texts = {
            "store_stop": "Add grocery store stop to the trip",
            "special_requests": "Customer special requests and notes",
            "need_carseats": "Check if customer needs car seats",
        }

    def clean(self):
        cleaned_data = super().clean()
        
        # NO VEHICLE CONSTRAINTS FOR DISPATCHERS
        # Dispatchers have full flexibility to override normal restrictions
        # They can add as many carseats as needed for any vehicle
        
        return cleaned_data

    def save(self, commit=True, **kwargs):
        """Save reservation without pricing (pricing handled separately)"""
        reservation = super().save(commit=False)
        
        # Set vehicle
        reservation.vehicle = self.cleaned_data['manual_vehicle']
        
        # Set other required fields from kwargs
        reservation.customer = kwargs.get('customer')
        reservation.trip_type = kwargs.get('trip_type', 'one_way')
        
        # Pricing will be set later in the pricing step
        reservation.base_price = kwargs.get('base_price', Decimal('0.00'))
        reservation.additional_charges = kwargs.get('additional_charges', Decimal('0.00'))
        reservation.total_price = reservation.base_price + reservation.additional_charges
        
        # Create a dummy rate if needed (for system compatibility)
        rate = kwargs.get('rate')
        if not rate:
            # Try to find existing rate or create a placeholder
            try:
                rate = Rate.objects.filter(
                    vehicle=reservation.vehicle
                ).first()
                if rate:
                    reservation.rate = rate
                else:
                    # This shouldn't happen in normal operation
                    # but provides fallback
                    pass
            except:
                pass
        else:
            reservation.rate = rate
            
        if commit:
            reservation.save()
        return reservation


class DispatcherLegForm(forms.ModelForm):
    """Dispatcher form for trip leg details"""

    class Meta:
        model = Leg
        fields = [
            "pickup_date", 
            "pickup_time", 
            "pickup_location", 
            "dropoff_location",
            "private_notes",
            "driver_pay_amount"
        ]
        labels = {
            "pickup_date": "Pickup Date",
            "pickup_time": "Pickup Time",
            "pickup_location": "Pickup Address",
            "dropoff_location": "Drop-off Address",
            "private_notes": "Trip Notes",
            "driver_pay_amount": "Driver Pay ($)",
        }
        widgets = {
            "pickup_date": forms.DateInput(
                attrs={
                    "type": "date", 
                    "class": "form-control form-control-lg",
                    "autocomplete": "off"
                }
            ),
            "pickup_time": forms.TimeInput(
                attrs={
                    "type": "time", 
                    "class": "form-control form-control-lg",
                    "autocomplete": "off"
                }
            ),
            "pickup_location": forms.TextInput(
                attrs={
                    "class": "form-control form-control-lg",
                    "placeholder": "e.g., 123 Main St, Orlando, FL 32819"
                }
            ),
            "dropoff_location": forms.TextInput(
                attrs={
                    "class": "form-control form-control-lg",
                    "placeholder": "e.g., Disney's Grand Floridian Resort"
                }
            ),
            "private_notes": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 2,
                    "placeholder": "Special instructions for this leg..."
                }
            ),
            "driver_pay_amount": forms.NumberInput(
                attrs={
                    "class": "form-control form-control-lg",
                    "placeholder": "0.00",
                    "step": "0.01",
                    "min": "0",
                }
            ),
        }
        help_texts = {
            "pickup_date": "Date of pickup",
            "pickup_time": "Time of pickup",
            "pickup_location": "Full pickup address",
            "dropoff_location": "Full drop-off address", 
            "private_notes": "Special instructions for driver",
            "driver_pay_amount": "Amount to pay driver for this leg",
        }

    def clean_pickup_date(self):
        date = self.cleaned_data["pickup_date"]
        if date < timezone.now().date():
            raise forms.ValidationError("Pickup date cannot be in the past")
        return date


class DispatcherFlightForm(forms.ModelForm):
    """Dispatcher form for flight information"""

    class Meta:
        model = Flight
        fields = ["airline", "flight_number", "flight_type"]
        widgets = {
            "airline": forms.TextInput(
                attrs={
                    "class": "form-control form-control-lg",
                    "placeholder": "e.g., Southwest, Delta, JetBlue",
                    "list": "airlines"
                }
            ),
            "flight_number": forms.TextInput(
                attrs={
                    "class": "form-control form-control-lg",
                    "placeholder": "e.g., SW1234, DL567"
                }
            ),
            "flight_type": forms.Select(
                attrs={"class": "form-control form-control-lg"}
            ),
        }
        labels = {
            "airline": "Airline",
            "flight_number": "Flight Number",
            "flight_type": "Flight Type",
        }
        help_texts = {
            "airline": "Airline name",
            "flight_number": "Flight number including airline code",
            "flight_type": "Arrival or Departure",
        }


# Formset for multiple legs
from django.forms import formset_factory

DispatcherLegFormSet = formset_factory(
    DispatcherLegForm,
    extra=1,
    max_num=5,
    min_num=1,
    can_delete=True,
    validate_min=True
)

DispatcherFlightFormSet = formset_factory(
    DispatcherFlightForm,
    extra=1,
    max_num=5,
    min_num=0,
    can_delete=True
)


class DispatcherPricingForm(forms.Form):
    """Separate form for pricing after legs are defined"""
    
    manual_base_price = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=True,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control form-control-lg",
                "placeholder": "0.00",
                "step": "0.01",
                "min": "0",
            }
        ),
        label="Base Price ($)",
        help_text="Set the base price for this reservation"
    )
    
    additional_charges = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        initial=0,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control form-control-lg",
                "placeholder": "0.00",
                "step": "0.01",
                "min": "0",
            }
        ),
        label="Additional Charges ($)",
        help_text="Any additional charges beyond base price"
    )
    
    gratuity_option = forms.ChoiceField(
        choices=[('none', 'No Gratuity'), ('20', '20%'), ('custom', 'Custom')],
        initial='none',
        required=False,
        widget=forms.RadioSelect(attrs={"class": "form-check-input"}),
        label="Gratuity",
    )

    gratuity_amount = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        initial=0,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control form-control-lg",
                "placeholder": "0.00",
                "step": "0.01",
                "min": "0",
            }
        ),
        label="Gratuity Amount ($)",
        help_text="Gratuity amount to include in the total price"
    )

    private_notes = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Private dispatcher notes (not visible to customer)...",
            }
        ),
        required=False,
        label="Private Notes",
        help_text="Internal notes not visible to customer"
    )

    def clean(self):
        cleaned_data = super().clean()
        base_price = cleaned_data.get('manual_base_price')
        additional_charges = cleaned_data.get('additional_charges', Decimal('0.00'))
        gratuity = cleaned_data.get('gratuity_amount') or Decimal('0.00')
        if base_price is not None:
            cleaned_data['total_price'] = base_price + additional_charges + gratuity
        return cleaned_data


class TripTypeForm(forms.Form):
    """Form to select trip type for dispatcher booking"""
    
    TRIP_CHOICES = [
        ('one_way', 'One Way'),
        ('round_trip', 'Round Trip'),
        ('multi_leg', 'Multiple Legs'),
    ]
    
    trip_type = forms.ChoiceField(
        choices=TRIP_CHOICES,
        widget=forms.RadioSelect(
            attrs={"class": "form-check-input"}
        ),
        label="Trip Type",
        initial='one_way'
    )
    
    num_legs = forms.IntegerField(
        min_value=1,
        max_value=5,
        initial=1,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control form-control-lg",
                "min": "1",
                "max": "5",
                # Removed inline style - visibility controlled by JavaScript
            }
        ),
        label="Number of Legs",
        help_text="How many separate trips for this reservation?",
        required=False
    )
