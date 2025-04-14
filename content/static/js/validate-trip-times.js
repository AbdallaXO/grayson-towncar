document.addEventListener("DOMContentLoaded", function () {
    const fields = {
        date1: document.getElementById("id_leg1-pickup_date"),
        time1: document.getElementById("id_leg1-pickup_time"),
        date2: document.getElementById("id_leg2-pickup_date"),
        time2: document.getElementById("id_leg2-pickup_time"),
    };

    const today = new Date();
    today.setHours(0, 0, 0, 0);

    function injectError(input, message) {
        let errorEl = input.parentNode.querySelector(".js-error");
        if (!errorEl) {
            errorEl = document.createElement("div");
            errorEl.className = "text-danger small mt-1 js-error";
            input.parentNode.appendChild(errorEl);
        }
        input.classList.add("is-invalid");
        errorEl.textContent = message;
    }

    function clearError(input) {
        input.classList.remove("is-invalid");
        const errorEl = input.parentNode.querySelector(".js-error");
        if (errorEl) errorEl.textContent = "";
    }

    function parseDate(input) {
        const val = input?.value;
        if (!val) return null;
        return new Date(val + "T00:00:00");
    }

    function parseTime(input) {
        const val = input?.value;
        if (!val) return null;
        const [h, m] = val.split(":").map(Number);
        const d = new Date();
        d.setHours(h, m, 0, 0);
        return d;
    }

    function validatePickup1Date() {
        const date = parseDate(fields.date1);
        if (!date) return;

        if (date < today) {
            injectError(fields.date1, "Pickup date cannot be in the past.");
        } else {
            clearError(fields.date1);
        }
    }

    function validatePickup1Time() {
        const date = parseDate(fields.date1);
        const time = parseTime(fields.time1);
        if (!date || !time) return;

        const now = new Date();
        if (date.toDateString() === now.toDateString() && time < now) {
            injectError(fields.time1, "Pickup time cannot be in the past.");
        } else {
            clearError(fields.time1);
        }
    }

    function validateReturnDateAfterFirst() {
        const d1 = parseDate(fields.date1);
        const d2 = parseDate(fields.date2);
        if (!d1 || !d2) return;

        if (d2 < d1) {
            injectError(fields.date2, "Return date cannot be before the first pickup date.");
        } else {
            clearError(fields.date2);
        }
    }

    function validateReturnTimeAfterFirstIfSameDay() {
        const date1 = parseDate(fields.date1);
        const time1 = parseTime(fields.time1);
        const date2 = parseDate(fields.date2);
        const time2 = parseTime(fields.time2);

        if (!date1 || !time1 || !date2 || !time2) return;

        // If both trips are on the same day
        if (date1.toDateString() === date2.toDateString()) {
            if (time2 <= time1) {
                injectError(fields.time2, "Return time must be after the first pickup time.");
            } else {
                clearError(fields.time2);
            }
        } else {
            clearError(fields.time2);
        }
    }

    // --- Bind Events ---
    if (fields.date1) {
        fields.date1.addEventListener("change", () => {
            validatePickup1Date();
            validatePickup1Time();
            validateReturnDateAfterFirst();
        });
    }

    if (fields.time1) {
        fields.time1.addEventListener("change", validatePickup1Time);
    }

    if (fields.date2) {
        fields.date2.addEventListener("change", validateReturnDateAfterFirst);
    }
    if (fields.time2) {
        fields.time2.addEventListener("change", validateReturnTimeAfterFirstIfSameDay);
    }
    if (fields.date2) {
        fields.date2.addEventListener("change", validateReturnTimeAfterFirstIfSameDay);
    }
    if (fields.date1) {
        fields.date1.addEventListener("change", validateReturnTimeAfterFirstIfSameDay);
    }
    if (fields.time1) {
        fields.time1.addEventListener("change", validateReturnTimeAfterFirstIfSameDay);
    }
});