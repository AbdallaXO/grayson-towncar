// static/js/quote_form.js
// Universal Quote Form Handler - supports multiple forms on the same page

class QuoteFormHandler {
    constructor(formElement) {
        this.form = formElement;
        this.formId = formElement.id;
        this.rateData = window.quoteFormRates[this.formId] || {};
        this.endpoint = formElement.dataset.endpoint;

        // Get all elements within this form's container
        this.container = formElement.closest('.quote-form-container');
        this.elements = this.getElements();

        this.init();
    }

    getElements() {
        return {
            pickup: this.container.querySelector('.pickup-select'),
            dropoff: this.container.querySelector('.dropoff-select'),
            vehicle: this.container.querySelector('.vehicle-select'),
            pickupDate: this.container.querySelector('input[name="pickup_date"]'), // ← Add this line
            quoteBtn: this.container.querySelector('.quote-btn'),
            displayContainer: this.container.querySelector('.quote-display-container'),
            invalidQuoteContainer: this.container.querySelector('.invalid-quote-container'),
            quoteDisplay: this.container.querySelector('.quote-display'),
            routeInfo: this.container.querySelector('.route-info'),
            vehiclePreview: this.container.querySelector('.vehicle-preview'),
            vehiclePlaceholder: this.container.querySelector('.vehicle-placeholder'),
            vehicleImage: this.container.querySelector('.vehicle-image'),
            vehicleName: this.container.querySelector('.vehicle-name'),
            passengerCapacity: this.container.querySelector('.passenger-capacity'),
            luggageCapacity: this.container.querySelector('.luggage-capacity'),
            carSeatsDisplay: this.container.querySelector('.carseats-display'),
            onewayBtn: this.container.querySelector('.oneway-btn'),
            roundtripBtn: this.container.querySelector('.roundtrip-btn'),
            formFields: this.container.querySelectorAll('.form-field'),
            tripRadios: this.container.querySelectorAll('.trip-radio')
        };
    }

    init() {
        this.bindEvents();
    }

    bindEvents() {
        // Vehicle selection
        this.elements.vehicle.addEventListener('change', () => {
            this.updateVehiclePreview();
            this.resetQuote();
        });

        // Location selection
        [this.elements.pickup, this.elements.dropoff].forEach(element => {
            element.addEventListener('change', () => {
                this.resetQuote();
                this.validateLocations();
                this.updateValidationState(element);
            });
        });

        // Form field validation
        this.elements.formFields.forEach(field => {
            field.addEventListener('input', () => {
                this.validateField(field);
            });
        });

        // Pickup date validation (if element exists)
        if (this.elements.pickupDate) {
            this.elements.pickupDate.addEventListener('input', () => {
                this.validateDateField(this.elements.pickupDate);
            });
        }

        // Trip type selection
        this.elements.tripRadios.forEach(radio => {
            radio.addEventListener('change', () => {
                this.resetQuote();
            });
        });

        // Form submission
        this.form.addEventListener('submit', (e) => {
            e.preventDefault();
            if (this.validateForm()) {
                this.getQuote();
            }
        });
    }

    updateVehiclePreview() {
        const selectedOption = this.elements.vehicle.options[this.elements.vehicle.selectedIndex];

        if (selectedOption.value) {
            this.elements.vehiclePreview.classList.remove('d-none');
            this.elements.vehiclePlaceholder.classList.add('d-none');

            let imageUrl = selectedOption.dataset.image;
            if (!imageUrl.startsWith('http')) {
                imageUrl = window.location.origin + imageUrl;
            }

            this.elements.vehicleImage.src = imageUrl;
            this.elements.vehicleName.textContent = selectedOption.text;
            this.elements.passengerCapacity.textContent = `${selectedOption.dataset.passengers} Passengers`;
            this.elements.luggageCapacity.textContent = `${selectedOption.dataset.luggage} Suitcases`;
            this.elements.carSeatsDisplay.textContent = `${selectedOption.dataset.carseats || 'N/A'} Available`;
        } else {
            this.elements.vehiclePreview.classList.add('d-none');
            this.elements.vehiclePlaceholder.classList.remove('d-none');
        }
    }

    validateLocations() {
        if (this.elements.pickup.value && this.elements.dropoff.value &&
            this.elements.pickup.value === this.elements.dropoff.value) {

            this.elements.dropoff.classList.add('is-invalid');
            this.elements.dropoff.classList.remove('is-valid');

            // Add or update error message
            let errorDiv = this.elements.dropoff.parentNode.querySelector('.invalid-feedback');
            if (!errorDiv) {
                errorDiv = document.createElement('div');
                errorDiv.className = 'invalid-feedback d-block';
                this.elements.dropoff.parentNode.appendChild(errorDiv);
            }
            errorDiv.textContent = 'Pickup and dropoff locations cannot be the same';
        } else {
            this.elements.dropoff.classList.remove('is-invalid');
            const errorDiv = this.elements.dropoff.parentNode.querySelector('.invalid-feedback');
            if (errorDiv) {
                errorDiv.remove();
            }
        }
    }

    updateValidationState(element) {
        if (element.value) {
            element.classList.remove('is-invalid');
            element.classList.add('is-valid');
        } else {
            element.classList.remove('is-valid');
        }
    }

    validateField(field) {
        const value = field.value.trim();
        if (!value) {
            field.classList.add('is-invalid');
            field.classList.remove('is-valid');
            return false;
        }

        if (field.checkValidity()) {
            field.classList.add('is-valid');
            field.classList.remove('is-invalid');
            return true;
        } else {
            field.classList.add('is-invalid');
            field.classList.remove('is-valid');
            return false;
        }
    }

    validateDateField(field) {
        const value = field.value;
        if (!value) {
            // Date is optional, so empty is valid
            field.classList.remove('is-invalid', 'is-valid');
            return true;
        }

        // Check if it's a valid date format and not in the past
        const selectedDate = new Date(value);
        const today = new Date();
        today.setHours(0, 0, 0, 0); // Reset time to start of day

        if (selectedDate < today) {
            field.classList.add('is-invalid');
            field.classList.remove('is-valid');
            return false;
        } else {
            field.classList.add('is-valid');
            field.classList.remove('is-invalid');
            return true;
        }
    }

    validateForm() {
        let isValid = true;
        let firstInvalidField = null;

        // Validate text inputs
        this.elements.formFields.forEach(field => {
            if (!this.validateField(field)) {
                isValid = false;
                if (!firstInvalidField) firstInvalidField = field;
            }
        });

        // Validate date field if it exists
        if (this.elements.pickupDate && !this.validateDateField(this.elements.pickupDate)) {
            isValid = false;
            if (!firstInvalidField) firstInvalidField = this.elements.pickupDate;
        }

        // Validate selects
        [this.elements.pickup, this.elements.dropoff, this.elements.vehicle].forEach(select => {
            if (!select.value) {
                select.classList.add('is-invalid');
                isValid = false;
                if (!firstInvalidField) firstInvalidField = select;
            } else {
                select.classList.remove('is-invalid');
                select.classList.add('is-valid');
            }
        });

        // Check for same pickup/dropoff
        if (this.elements.pickup.value === this.elements.dropoff.value && this.elements.pickup.value) {
            isValid = false;
            if (!firstInvalidField) firstInvalidField = this.elements.dropoff;
        }

        if (!isValid && firstInvalidField) {
            firstInvalidField.scrollIntoView({ behavior: 'smooth', block: 'center' });
            firstInvalidField.focus();
        }

        return isValid;
    }

    setLoading(isLoading) {
        if (isLoading) {
            this.elements.quoteBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status"></span>Getting Quote...';
            this.elements.quoteBtn.disabled = true;
        } else {
            this.elements.quoteBtn.innerHTML = '<i class="bi bi-calculator me-2"></i>REQUEST MY QUOTE NOW';
            this.elements.quoteBtn.disabled = false;
        }
    }

    setSuccess() {
        this.elements.quoteBtn.innerHTML = '<i class="bi bi-check-circle me-2"></i>Quote Generated!';
        this.elements.quoteBtn.classList.remove('btn-dark');
        this.elements.quoteBtn.classList.add('btn-dark');
    }

    showQuote(price, route, onewayUrl, roundtripUrl) {
        this.elements.displayContainer.classList.remove('d-none');
        this.elements.invalidQuoteContainer.classList.add('d-none');
        this.elements.quoteDisplay.textContent = `$${price}`;
        this.elements.routeInfo.textContent = route;
        this.elements.onewayBtn.href = onewayUrl;
        this.elements.roundtripBtn.href = roundtripUrl;

        // Calculate and display savings for round-trip
        const currentTripType = this.container.querySelector('input[name^="trip"]:checked').value;
        const locationIds = [this.elements.pickup.value, this.elements.dropoff.value].sort();
        const key = `${locationIds[0]}-${locationIds[1]}`;
        const rate = this.rateData[this.elements.vehicle.value]?.[key];

        if (rate) {
            const onewayPrice = rate.oneway;
            const roundtripPrice = rate.round;
            const savings = onewayPrice * 2 - roundtripPrice;

            if (currentTripType === '1') {
                // Show one-way price with round-trip savings
                if (savings > 0) {
                    this.elements.roundtripBtn.innerHTML = `<i class="bi bi-arrow-repeat me-2"></i>Book Round-trip & Save $${savings}`;
                } else {
                    this.elements.roundtripBtn.innerHTML = `<i class="bi bi-arrow-repeat me-2"></i>Book Round-trip`;
                }
            } else {
                // Show round-trip price with savings
                this.elements.onewayBtn.innerHTML = `<i class="bi bi-arrow-right me-2"></i>Book One-way Now`;
                if (savings > 0) {
                    this.elements.roundtripBtn.innerHTML = `<i class="bi bi-arrow-repeat me-2"></i>Book Round-trip & Save $${savings}`;
                } else {
                    this.elements.roundtripBtn.innerHTML = `<i class="bi bi-arrow-repeat me-2"></i>Book Round-trip`;
                }
            }
        }

        this.elements.displayContainer.scrollIntoView({ behavior: 'smooth', block: 'center' });

        // Add a subtle animation to draw attention
        setTimeout(() => {
            this.elements.displayContainer.style.transform = 'scale(1.02)';
            setTimeout(() => {
                this.elements.displayContainer.style.transform = 'scale(1)';
            }, 200);
        }, 100);
    }

    showInvalidQuote() {
        this.elements.displayContainer.classList.add('d-none');
        this.elements.invalidQuoteContainer.classList.remove('d-none');
        this.elements.quoteBtn.innerHTML = '<i class="bi bi-calculator me-2"></i>REQUEST MY QUOTE NOW';
        this.elements.quoteBtn.classList.remove('btn-dark');
        this.elements.quoteBtn.classList.add('btn-dark');
        this.elements.invalidQuoteContainer.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    resetQuote() {
        this.elements.quoteDisplay.textContent = '';
        this.elements.routeInfo.textContent = '';
        this.elements.displayContainer.classList.add('d-none');
        this.elements.invalidQuoteContainer.classList.add('d-none');
        this.elements.quoteBtn.innerHTML = '<i class="bi bi-calculator me-2"></i>REQUEST MY QUOTE NOW';
        this.elements.quoteBtn.classList.remove('btn-dark');
        this.elements.quoteBtn.classList.add('btn-dark');
    }

    async getQuote() {
        const pickup = this.elements.pickup.value;
        const dropoff = this.elements.dropoff.value;
        const vehicle = this.elements.vehicle.value;
        const tripType = this.container.querySelector('input[name^="trip"]:checked').value;

        if (!pickup || !dropoff || !vehicle) {
            this.resetQuote();
            return;
        }

        this.setLoading(true);

        try {
            // Prepare form data
            const formData = {};
            this.elements.formFields.forEach(field => {
                formData[field.name] = field.value;
            });

            // Add pickup_date if it exists
            if (this.elements.pickupDate) {
                formData.pickup_date = this.elements.pickupDate.value || '';
            }

            const leadData = {
                ...formData,
                vehicle_id: vehicle,
                pickup_location: this.elements.pickup.options[this.elements.pickup.selectedIndex].text,
                dropoff_location: this.elements.dropoff.options[this.elements.dropoff.selectedIndex].text,
                trip_type: tripType,
                estimated_price: null
            };

            // Check for rate
            const locationIds = [pickup, dropoff].sort();
            const key = `${locationIds[0]}-${locationIds[1]}`;
            const rate = this.rateData[vehicle]?.[key];

            if (rate) {
                leadData.estimated_price = tripType === '1' ? rate.oneway : rate.round;
            }

            // Submit the lead
            const response = await fetch(this.endpoint, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': this.getCookie('csrftoken')
                },
                body: JSON.stringify(leadData)
            });

            const data = await response.json();

            if (data.success) {
                if (rate) {
                    const price = tripType === '1' ? rate.oneway : rate.round;
                    // Include customer information in the booking URLs for autofill
                    const customerParams = new URLSearchParams({
                        first_name: formData.first_name || '',
                        last_name: formData.last_name || '',
                        email: formData.email || '',
                        phone: formData.phone || ''
                    }).toString();

                    const onewayUrl = `/book-orlando-transportation/${rate.id}?round=1&${customerParams}`;
                    const roundtripUrl = `/book-orlando-transportation/${rate.id}?round=2&${customerParams}`;
                    this.showQuote(price, `${rate.origin} → ${rate.destination}`, onewayUrl, roundtripUrl);
                    this.setSuccess();
                } else {
                    this.showInvalidQuote();
                }
            } else {
                throw new Error('Failed to create lead');
            }
        } catch (error) {
            console.error('Error:', error);
            this.showInvalidQuote();
        } finally {
            this.setLoading(false);
        }
    }

    getCookie(name) {
        const cookies = document.cookie.split(';');
        for (let cookie of cookies) {
            const [cookieName, cookieValue] = cookie.trim().split('=');
            if (cookieName === name) {
                return decodeURIComponent(cookieValue);
            }
        }
        return null;
    }
}

// Auto-initialize all quote forms when DOM is ready
document.addEventListener('DOMContentLoaded', function () {
    const quoteForms = document.querySelectorAll('.quote-form');
    quoteForms.forEach(form => {
        new QuoteFormHandler(form);
    });
});