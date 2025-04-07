// Enhanced Form Validation and Dynamic Behavior for Django Towncar Reservation

document.addEventListener('DOMContentLoaded', function() {
  // Form and Input Selectors
  const customerForm = document.querySelector('#customer-form');
  const reservationForm = document.querySelector('#reservation-form');
  const legForm = document.querySelector('#leg-form');
  const flightForm = document.querySelector('#flight-form');
  
  const hasChildrenCheckbox = document.querySelector('#id_has_children');
  const carSeatTypeSelect = document.querySelector('#id_carseat_type');
  const passengerCountInput = document.querySelector('#id_passenger_count');
  const luggageCountInput = document.querySelector('#id_luggage_count');
  const pickupDateInputs = document.querySelectorAll('input[type="date"]');
  const specialRequestsTextarea = document.querySelector('#id_special_requests');

  // Email Validation
  function validateEmail(email) {
      const re = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;
      return re.test(String(email).toLowerCase());
  }

  // Phone Number Validation
  function validatePhoneNumber(phone) {
      const re = /^\d{3}-\d{3}-\d{4}$/;
      return re.test(phone);
  }

  // Dynamic Form Validation
  function enhanceFormValidation(form) {
      if (!form) return;

      form.addEventListener('submit', function(e) {
          let isValid = true;
          const requiredFields = form.querySelectorAll('[required], .form-control');

          requiredFields.forEach(field => {
              // Clear previous error states
              field.classList.remove('is-invalid');
              
              // Specific validations
              if (field.hasAttribute('required') && !field.value.trim()) {
                  field.classList.add('is-invalid');
                  isValid = false;
              }

              // Email validation
              if (field.type === 'email' && field.value.trim()) {
                  if (!validateEmail(field.value)) {
                      field.classList.add('is-invalid');
                      isValid = false;
                  }
              }

              // Phone number validation
              if (field.name === 'phone_number' && field.value.trim()) {
                  if (!validatePhoneNumber(field.value)) {
                      field.classList.add('is-invalid');
                      isValid = false;
                  }
              }
          });

          // Prevent form submission if invalid
          if (!isValid) {
              e.preventDefault();
              // Optional: Scroll to first invalid field
              const firstInvalidField = form.querySelector('.is-invalid');
              if (firstInvalidField) {
                  firstInvalidField.focus();
              }
          }
      });
  }

  // Dynamic Car Seat Type Management
  function manageCarseatType() {
      if (hasChildrenCheckbox && carSeatTypeSelect) {
          // Initially disable car seat type if no children
          carSeatTypeSelect.disabled = !hasChildrenCheckbox.checked;

          hasChildrenCheckbox.addEventListener('change', function() {
              carSeatTypeSelect.disabled = !this.checked;
              
              if (!this.checked) {
                  carSeatTypeSelect.selectedIndex = 0; // Reset selection
              }
          });
      }
  }

  // Luggage Recommendations
  function updateLuggageRecommendations() {
      if (passengerCountInput && luggageCountInput) {
          const passengerCount = parseInt(passengerCountInput.value) || 1;
          const recommendedLuggage = Math.min(passengerCount * 2, 12); // Max 12 bags

          luggageCountInput.setAttribute('max', recommendedLuggage);
          luggageCountInput.setAttribute('title', `Recommended: Up to ${recommendedLuggage} bags`);

          // Optional: Add a hint near the luggage input
          const luggageHint = document.createElement('small');
          luggageHint.classList.add('form-text', 'text-muted');
          luggageHint.textContent = `Recommended: Up to ${recommendedLuggage} bags`;
          luggageCountInput.parentNode.appendChild(luggageHint);
      }
  }

  // Prevent Past Dates
  function preventPastDates() {
      if (pickupDateInputs.length) {
          const today = new Date().toISOString().split('T')[0];
          pickupDateInputs.forEach(input => {
              input.setAttribute('min', today);
          });
      }
  }

  // Special Requests Character Limit
  function manageSpecialRequestsLimit() {
      if (specialRequestsTextarea) {
          const maxLength = 500;
          specialRequestsTextarea.setAttribute('maxlength', maxLength);

          const charCountDisplay = document.createElement('small');
          charCountDisplay.classList.add('form-text', 'text-muted');
          specialRequestsTextarea.parentNode.appendChild(charCountDisplay);

          specialRequestsTextarea.addEventListener('input', function() {
              const remainingChars = maxLength - this.value.length;
              charCountDisplay.textContent = `${remainingChars} characters remaining`;
          });
      }
  }

  // Form Dependent Validations
  function setupFormDependencies() {
      // Example: Ensure pickup location is filled if flight info is provided
      const airlineInput = document.querySelector('#id_airline');
      const flightNumberInput = document.querySelector('#id_flight_number');
      const pickupLocationInput = document.querySelector('#id_pickup_location');

      if (airlineInput && flightNumberInput && pickupLocationInput) {
          [airlineInput, flightNumberInput].forEach(input => {
              input.addEventListener('change', function() {
                  if (airlineInput.value.trim() || flightNumberInput.value.trim()) {
                      pickupLocationInput.setAttribute('required', 'required');
                  } else {
                      pickupLocationInput.removeAttribute('required');
                  }
              });
          });
      }
  }

  // Initialize all enhancements
  function initializeFormEnhancements() {
      // Validate forms
      [customerForm, reservationForm, legForm, flightForm].forEach(enhanceFormValidation);

      // Other enhancements
      manageCarseatType();
      updateLuggageRecommendations();
      preventPastDates();
      manageSpecialRequestsLimit();
      setupFormDependencies();
  }

  // Run enhancements
  initializeFormEnhancements();
});

// Companion CSS for validation
const styles = `
<style>
.is-invalid {
  border: 2px solid #dc3545;
  animation: shake 0.3s;
}

@keyframes shake {
  0%, 100% { transform: translateX(0); }
  25% { transform: translateX(-5px); }
  75% { transform: translateX(5px); }
}

/* Disable state for car seat type */
select:disabled {
  background-color: #f4f4f4;
  cursor: not-allowed;
}
</style>
`;

// Inject styles
document.head.insertAdjacentHTML('beforeend', styles);