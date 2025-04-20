document.addEventListener('DOMContentLoaded', function () {
    // Initialize Bootstrap tooltips
    var tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    var tooltipList = tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl);
    });

    // Handle car seats section visibility based on has_children checkbox
    var hasChildrenCheckbox = document.getElementById('id_need_carseats');
    var carSeatsSection = document.getElementById('car-seats-section');

    function toggleCarSeatsSection() {
        if (hasChildrenCheckbox && carSeatsSection) {
            carSeatsSection.style.display = hasChildrenCheckbox.checked ? 'block' : 'none';
        }
    }
    // Initial state
    toggleCarSeatsSection();
    if (hasChildrenCheckbox) {
        hasChildrenCheckbox.addEventListener('change', toggleCarSeatsSection);
    }

    // Enhanced form validation + highlight first invalid field
    const forms = document.querySelectorAll('.needs-validation');
    Array.from(forms).forEach(form => {
        form.addEventListener('submit', event => {
            if (!form.checkValidity()) {
                event.preventDefault();
                event.stopPropagation();

                // Scroll to first error
                const firstError = form.querySelector(':invalid');
                if (firstError) {
                    firstError.scrollIntoView({
                        behavior: 'smooth',
                        block: 'center'
                    });
                    firstError.classList.add('highlight-error');
                    setTimeout(() => {
                        firstError.classList.remove('highlight-error');
                    }, 1500);
                }
            }
            form.classList.add('was-validated');
        }, false);
    });

    // Animate form cards when they enter the viewport
    const formCards = document.querySelectorAll('.form-card');
    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.style.opacity = '1';
                entry.target.style.transform = 'translateY(0)';
                observer.unobserve(entry.target);
            }
        });
    }, {
        threshold: 0.2
    });

    formCards.forEach(card => {
        card.style.opacity = '0';
        card.style.transform = 'translateY(20px)';
        card.style.transition = 'opacity 0.5s ease, transform 0.5s ease';
        observer.observe(card);
    });
});