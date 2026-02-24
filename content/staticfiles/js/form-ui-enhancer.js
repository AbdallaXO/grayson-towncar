/**
 * Grayson Towncar — Form UI Enhancer
 * Handles: stepper controls, phone formatting, progress bar,
 * toggle cards, quick-tap chips, car seats, smooth animations
 */
document.addEventListener('DOMContentLoaded', function () {

    // ========== Bootstrap Tooltips ==========
    var tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.map(function (el) {
        return new bootstrap.Tooltip(el);
    });

    // ========== Stepper Controls (+/−) ==========
    // Car seat input IDs that support extra-seat upsell
    var seatInputIds = ['id_ff_carseats', 'id_rf_carseats', 'id_booster_seats'];

    document.querySelectorAll('.gt-stepper-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var targetId = this.getAttribute('data-target');
            var input = document.getElementById(targetId);
            if (!input) return;

            var current = parseInt(input.value, 10) || 0;
            var min = parseInt(input.getAttribute('min'), 10) || 0;
            var max = parseInt(input.getAttribute('max'), 10) || 99;
            var action = this.getAttribute('data-action');

            // For car seat fields, check extra seat upsell on every increment
            // (_extraSeatCheck decides internally whether the limit is reached)
            if (action === 'increment' && seatInputIds.indexOf(targetId) !== -1) {
                if (typeof window._extraSeatCheck === 'function') {
                    var handled = window._extraSeatCheck(targetId, action);
                    if (handled) return; // Modal shown, don't increment yet
                }
            }

            if (action === 'increment' && current < max) {
                input.value = current + 1;
            } else if (action === 'decrement' && current > min) {
                input.value = current - 1;
            }

            // Trigger change event for other validators
            input.dispatchEvent(new Event('change', { bubbles: true }));
            input.dispatchEvent(new Event('input', { bubbles: true }));

            // Update disabled state
            updateStepperBtnState(input);
        });
    });

    function updateStepperBtnState(input) {
        var stepper = input.closest('.gt-stepper');
        if (!stepper) return;
        var val = parseInt(input.value, 10) || 0;
        var min = parseInt(input.getAttribute('min'), 10) || 0;
        var max = parseInt(input.getAttribute('max'), 10) || 99;
        var inputId = input.id;
        var isSeatInput = seatInputIds.indexOf(inputId) !== -1;
        var btns = stepper.querySelectorAll('.gt-stepper-btn');
        btns.forEach(function (btn) {
            if (btn.getAttribute('data-action') === 'decrement') {
                btn.disabled = val <= min;
            } else {
                // For car seat inputs, never disable + — the upsell modal handles it
                btn.disabled = isSeatInput ? false : (val >= max);
            }
        });
    }

    // Initialize stepper button states
    document.querySelectorAll('.gt-stepper input').forEach(updateStepperBtnState);

    // ========== Phone Number Auto-Formatting ==========
    var phoneInput = document.querySelector('input[name="phone_number"]');
    if (phoneInput) {
        phoneInput.addEventListener('input', function (e) {
            var x = e.target.value.replace(/\D/g, '');
            // Limit to 10 digits
            x = x.substring(0, 10);
            var formatted = '';
            if (x.length > 6) {
                formatted = x.substring(0, 3) + '-' + x.substring(3, 6) + '-' + x.substring(6);
            } else if (x.length > 3) {
                formatted = x.substring(0, 3) + '-' + x.substring(3);
            } else {
                formatted = x;
            }
            e.target.value = formatted;
        });
    }

    // ========== Toggle Cards (Children & Grocery) ==========
    function setupToggleCard(toggleId, checkboxName, detailsId) {
        var card = document.getElementById(toggleId);
        var checkbox = document.querySelector('input[name="' + checkboxName + '"]');
        var details = detailsId ? document.getElementById(detailsId) : null;

        if (!card || !checkbox) return;

        function updateState() {
            if (checkbox.checked) {
                card.classList.add('active');
                if (details) details.style.display = 'block';
            } else {
                card.classList.remove('active');
                if (details) details.style.display = 'none';
            }
        }

        checkbox.addEventListener('change', updateState);
        updateState();
    }

    setupToggleCard('children-toggle', 'need_carseats', null);
    setupToggleCard('grocery-toggle', 'store_stop', 'grocery-details');

    // ========== Car Seats Section ==========
    var hasChildrenCheckbox = document.getElementById('id_need_carseats');
    var carSeatsSection = document.getElementById('car-seats-section');

    function toggleCarSeatsSection() {
        if (hasChildrenCheckbox && carSeatsSection) {
            carSeatsSection.style.display = hasChildrenCheckbox.checked ? 'block' : 'none';
        }
    }

    toggleCarSeatsSection();
    if (hasChildrenCheckbox) {
        hasChildrenCheckbox.addEventListener('change', toggleCarSeatsSection);
    }

    // ========== Sticky Progress Bar ==========
    var progressSteps = document.querySelectorAll('.gt-progress-step');
    var progressFill = document.getElementById('progressFill');
    var sections = document.querySelectorAll('[data-progress-step]');

    function updateProgress() {
        if (!sections.length || !progressSteps.length) return;

        var scrollY = window.scrollY || window.pageYOffset;
        var viewHeight = window.innerHeight;
        var activeStep = 1;

        sections.forEach(function (section) {
            var rect = section.getBoundingClientRect();
            var top = rect.top + scrollY;
            // Consider section "active" when its top is within the upper half of viewport
            if (scrollY + viewHeight * 0.3 >= top) {
                activeStep = parseInt(section.getAttribute('data-progress-step'), 10);
            }
        });

        var totalSteps = progressSteps.length;
        progressSteps.forEach(function (step) {
            var stepNum = parseInt(step.getAttribute('data-step'), 10);
            step.classList.remove('active', 'completed');
            if (stepNum === activeStep) {
                step.classList.add('active');
            } else if (stepNum < activeStep) {
                step.classList.add('completed');
            }
        });

        // Update fill bar width
        if (progressFill) {
            var pct = ((activeStep - 1) / (totalSteps - 1)) * 100;
            progressFill.style.width = pct + '%';
        }
    }

    // Smooth scroll when clicking progress steps
    progressSteps.forEach(function (step) {
        step.addEventListener('click', function (e) {
            e.preventDefault();
            var href = this.getAttribute('href');
            var target = document.querySelector(href);
            if (target) {
                var offset = 80; // Account for sticky bar
                var y = target.getBoundingClientRect().top + window.pageYOffset - offset;
                window.scrollTo({ top: y, behavior: 'smooth' });
            }
        });
    });

    // Throttled scroll handler
    var ticking = false;
    window.addEventListener('scroll', function () {
        if (!ticking) {
            window.requestAnimationFrame(function () {
                updateProgress();
                ticking = false;
            });
            ticking = true;
        }
    }, { passive: true });

    updateProgress();

    // ========== Input Focus Enhancement ==========
    document.querySelectorAll('.form-control, .form-select').forEach(function (input) {
        input.addEventListener('focus', function () {
            var row = this.closest('.form-row') || this.closest('.col-12, .col-md-6');
            if (row) row.classList.add('gt-field-focused');
        });
        input.addEventListener('blur', function () {
            var row = this.closest('.form-row') || this.closest('.col-12, .col-md-6');
            if (row) row.classList.remove('gt-field-focused');
        });
    });

    // ========== Form Validation + Scroll to First Error ==========
    // Clear any previous client-side validation messages
    function clearClientErrors(form) {
        form.querySelectorAll('.gt-client-error').forEach(function (el) { el.remove(); });
        form.querySelectorAll('.highlight-error').forEach(function (el) { el.classList.remove('highlight-error'); });
    }

    // Track radio groups that already have an error shown
    var radioGroupsHandled = {};

    // Show a visible error message below an invalid field
    function showClientError(input) {
        // Skip hidden inputs
        if (input.type === 'hidden') return;

        // For radio buttons, only show one error per group
        if (input.type === 'radio') {
            var groupName = input.name;
            if (radioGroupsHandled[groupName]) return;
            radioGroupsHandled[groupName] = true;

            // Find the card-select or radiogroup container
            var radioGroup = input.closest('.gt-card-select') || input.closest('[role="radiogroup"]');
            if (radioGroup) {
                if (radioGroup.querySelector('.gt-client-error')) return;
                var msg = 'Please select an option';
                var errorDiv = document.createElement('div');
                errorDiv.className = 'gt-client-error text-danger small mt-2';
                errorDiv.setAttribute('role', 'alert');
                errorDiv.innerHTML = '<i class="bi bi-exclamation-circle me-1" aria-hidden="true"></i>' + msg;
                radioGroup.parentNode.appendChild(errorDiv);
                radioGroup.classList.add('highlight-error');
                return;
            }
        }

        // Don't duplicate if we already added one
        var parent = input.closest('.gt-stepper') || input;
        var container = parent.parentNode;
        if (container.querySelector('.gt-client-error')) return;

        var msg = input.validationMessage || 'This field is required';
        var errorDiv = document.createElement('div');
        errorDiv.className = 'gt-client-error text-danger small mt-1';
        errorDiv.setAttribute('role', 'alert');
        errorDiv.innerHTML = '<i class="bi bi-exclamation-circle me-1" aria-hidden="true"></i>' + msg;
        container.appendChild(errorDiv);

        // Mark field visually
        input.classList.add('highlight-error');
    }

    var forms = document.querySelectorAll('.needs-validation');
    Array.from(forms).forEach(function (form) {
        form.addEventListener('submit', function (event) {
            clearClientErrors(form);
            radioGroupsHandled = {};

            if (!form.checkValidity()) {
                event.preventDefault();
                event.stopPropagation();

                // Show errors on ALL invalid fields
                var invalidFields = form.querySelectorAll(':invalid');
                invalidFields.forEach(function (field) {
                    showClientError(field);
                });

                // Scroll to first visible invalid field
                var firstVisible = null;
                for (var i = 0; i < invalidFields.length; i++) {
                    if (invalidFields[i].type !== 'hidden' && invalidFields[i].offsetParent !== null) {
                        firstVisible = invalidFields[i];
                        break;
                    }
                }
                if (firstVisible) {
                    firstVisible.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    firstVisible.focus();
                }
            }
            form.classList.add('was-validated');
        }, false);

        // Clear individual field errors on input/change
        function clearFieldError(input) {
            if (input.validity && input.validity.valid) {
                input.classList.remove('highlight-error');
                var parent = input.closest('.gt-stepper') || input;
                var container = parent.parentNode;
                var err = container.querySelector('.gt-client-error');
                if (err) err.remove();

                // For radios, clear the group error
                if (input.type === 'radio') {
                    var group = input.closest('.gt-card-select') || input.closest('[role="radiogroup"]');
                    if (group) {
                        group.classList.remove('highlight-error');
                        var groupErr = group.parentNode.querySelector('.gt-client-error');
                        if (groupErr) groupErr.remove();
                    }
                }
            }
        }
        form.addEventListener('input', function (e) { clearFieldError(e.target); });
        form.addEventListener('change', function (e) { clearFieldError(e.target); });
    });

    // ========== Animate Cards on Scroll (respects prefers-reduced-motion) ==========
    var prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    if (!prefersReducedMotion) {
        var formCards = document.querySelectorAll('.form-card');
        var observer = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (entry.isIntersecting) {
                    entry.target.style.opacity = '1';
                    entry.target.style.transform = 'translateY(0)';
                    observer.unobserve(entry.target);
                }
            });
        }, { threshold: 0.1 });

        formCards.forEach(function (card) {
            card.style.opacity = '0';
            card.style.transform = 'translateY(12px)';
            card.style.transition = 'opacity 0.4s ease, transform 0.4s ease';
            observer.observe(card);
        });
    }

    // ========== Cash Note Visibility on Page Load ==========
    var cashRadio = document.getElementById('gratuity_none');
    var cashNote = document.getElementById('cash_note');
    if (cashRadio && cashRadio.checked && cashNote) {
        cashNote.style.display = 'flex';
    }
});
