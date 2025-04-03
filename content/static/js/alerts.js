document.addEventListener('DOMContentLoaded', function () {
    setTimeout(function () {
        const notifications = document.querySelectorAll('.notification');
        notifications.forEach(function (notification) {
            notification.style.opacity = '0';
        });

        // Remove from DOM after transition completes
        setTimeout(function () {
            const wrapper = document.querySelector('.notification-wrapper');
            if (wrapper) {
                wrapper.remove();
            }
        }, 300);
    }, 1000);
});