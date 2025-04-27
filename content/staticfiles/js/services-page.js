document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".accordion-toggle").forEach(function (toggle) {
        toggle.addEventListener("click", function () {
            const targetId = this.getAttribute("data-target");
            const target = document.querySelector(targetId);
            target.classList.toggle("active");
        });
    });
});
// Only run on mobile devices
document.addEventListener('DOMContentLoaded', function () {
    if (window.innerWidth <= 767) {
        // Get all tab panels
        const tabPanes = document.querySelectorAll('.tab-pane');

        // Hide all tab panes except the active one
        tabPanes.forEach(pane => {
            if (!pane.classList.contains('show')) {
                pane.style.display = 'none';
            }
        });

        // Add click event to tabs
        const tabButtons = document.querySelectorAll('.nav-link');
        tabButtons.forEach(button => {
            button.addEventListener('click', function () {
                const targetId = this.getAttribute('data-bs-target');
                const targetPane = document.querySelector(targetId);

                // Toggle display of the clicked pane
                if (targetPane.style.display === 'none') {
                    tabPanes.forEach(pane => {
                        pane.style.display = 'none';
                    });
                    targetPane.style.display = 'block';
                }
            });
        });
    }
});
document.addEventListener("DOMContentLoaded", function () {
    const params = new URLSearchParams(window.location.search);
    const tab = params.get("tab");

    if (tab) {
        const tabButton = document.querySelector(`#${tab}-tab`);
        if (tabButton) {
            // Bootstrap 5 tab activation
            const tabTrigger = new bootstrap.Tab(tabButton);
            tabTrigger.show();
        }
    }
});