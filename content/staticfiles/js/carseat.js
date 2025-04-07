/**
 * Car Seat Counter JavaScript
 * Adds +/- counter functionality to existing car seat fields
 */
document.addEventListener('DOMContentLoaded', function() {
  // Setup car seat toggle
  setupCarSeatToggle();
  // Setup counter buttons
  setupCounterButtons();
});

/**
 * Car Seat Toggle Function
 * Shows/hides car seat fields based on the checkbox
 */
function setupCarSeatToggle() {
  const needCarseatsCheckbox = document.getElementById('id_need_carseats');
  const carseatRows = document.querySelectorAll('.carseat-row');
  
  if (needCarseatsCheckbox && carseatRows.length > 0) {
    // Toggle visibility on change
    needCarseatsCheckbox.addEventListener('change', function() {
      const displayValue = this.checked ? 'flex' : 'none';
      
      carseatRows.forEach(row => {
        row.style.display = displayValue;
      });
      
      // Reset counters when hiding
      if (!this.checked) {
        document.querySelectorAll('#id_rf_carseat, #id_ff_carseat, #id_booster_seats').forEach(input => {
          input.value = 0;
        });
      }
    });
    
    // Initial state
    const initialDisplay = needCarseatsCheckbox.checked ? 'flex' : 'none';
    carseatRows.forEach(row => {
      row.style.display = initialDisplay;
    });
  }
}

/**
 * Counter Buttons Setup
 * Adds +/- buttons to number inputs for car seats
 */
function setupCounterButtons() {
  // Find all car seat counter inputs
  const counterInputs = document.querySelectorAll('#id_rf_carseat, #id_ff_carseat, #id_booster_seats');
  
  counterInputs.forEach(input => {
    // Create container for the counter buttons
    const container = document.createElement('div');
    container.className = 'd-flex align-items-center counter-container';
    
    // Get max value from input attributes
    const maxValue = parseInt(input.getAttribute('max') || 2);
    
    // Create decrement button
    const decrementBtn = document.createElement('button');
    decrementBtn.type = 'button';
    decrementBtn.className = 'btn btn-outline-secondary counter-btn';
    decrementBtn.innerHTML = '−'; // Using minus sign character
    decrementBtn.addEventListener('click', function(e) {
      e.preventDefault();
      const currentValue = parseInt(input.value) || 0;
      if (currentValue > 0) {
        input.value = currentValue - 1;
        // Trigger change event for any listeners
        input.dispatchEvent(new Event('change'));
      }
    });
    
    // Create increment button
    const incrementBtn = document.createElement('button');
    incrementBtn.type = 'button';
    incrementBtn.className = 'btn btn-outline-secondary counter-btn';
    incrementBtn.innerHTML = '+';
    incrementBtn.addEventListener('click', function(e) {
      e.preventDefault();
      const currentValue = parseInt(input.value) || 0;
      if (currentValue < maxValue) {
        input.value = currentValue + 1;
        // Trigger change event for any listeners
        input.dispatchEvent(new Event('change'));
      }
    });
    
    // Add original input and counter buttons to container
    const originalLabel = input.closest('.form-check').querySelector('label');
    const checkDiv = input.closest('.form-check');
    
    // Add 'carseat-row' class to the parent for toggling visibility
    checkDiv.classList.add('carseat-row');
    
    // Clear the check div
    checkDiv.innerHTML = '';
    
    // Create a label with the original text
    const newLabel = document.createElement('label');
    newLabel.className = 'form-check-label me-2';
    newLabel.innerHTML = originalLabel.innerHTML;
    
    // Add the label to the check div
    checkDiv.appendChild(newLabel);
    
    // Add the counter container
    checkDiv.appendChild(container);
    
    // Style the input
    input.className = 'form-control counter-input';
    input.style.width = '50px';
    input.min = 0;
    input.max = maxValue;
    
    // Add elements to container
    container.appendChild(decrementBtn);
    container.appendChild(input);
    container.appendChild(incrementBtn);
  });
  
  // Initialize with the checkbox state
  const needCarseatsCheckbox = document.getElementById('id_need_carseats');
  if (needCarseatsCheckbox) {
    const initialDisplay = needCarseatsCheckbox.checked ? 'flex' : 'none';
    document.querySelectorAll('.carseat-row').forEach(row => {
      row.style.display = initialDisplay;
    });
  }
}