    // Vehicle Capacity Validation Script

document.addEventListener("DOMContentLoaded", function () {
    const limitsEl = document.getElementById("vehicle-limits");
    if (!limitsEl) return;

    const limits = {
        passengers: parseInt(limitsEl.dataset.maxPassengers),
        luggage: parseInt(limitsEl.dataset.maxLuggage),
        ff: parseInt(limitsEl.dataset.maxFf),
        rf: parseInt(limitsEl.dataset.maxRf),
        booster: parseInt(limitsEl.dataset.maxBoosters),
        carseats: parseInt(limitsEl.dataset.maxCarseats),
    };

    const inputs = {
        passenger: document.getElementById("id_passenger_count"),
        luggage: document.getElementById("id_luggage_count"),
        ff: document.getElementById("id_ff_carseats"),
        rf: document.getElementById("id_rf_carseats"),
        booster: document.getElementById("id_booster_seats"),
    };

    const errors = {};

    function createOrGetErrorElement(input, key) {
        if (errors[key]) return errors[key];

        const errorEl = document.createElement("div");
        errorEl.classList.add("text-danger", "small", "mt-1");
        errorEl.id = `error-${key}`;
        input.parentNode.appendChild(errorEl);
        errors[key] = errorEl;
        return errorEl;
    }

    function showError(input, key, message) {
        input.classList.add("is-invalid");
        const errorEl = createOrGetErrorElement(input, key);
        errorEl.textContent = message;
    }

    function clearError(input, key) {
        input.classList.remove("is-invalid");
        const errorEl = errors[key];
        if (errorEl) errorEl.textContent = "";
    }

    function validateSingleField(input, key, max, label) {
        const value = parseInt(input.value || 0);
        if (value > max) {
            input.value = max;
            showError(input, key, `${label} cannot exceed ${max}`);
        } else {
            clearError(input, key);
        }
    }

    function validateTotalCarseats() {
        const ff = parseInt(inputs.ff?.value || 0);
        const rf = parseInt(inputs.rf?.value || 0);
        const booster = parseInt(inputs.booster?.value || 0);
        const total = ff + rf + booster;

        if (total > limits.carseats) {
            const msg = `Total car seats cannot exceed ${limits.carseats}`;
            [inputs.ff, inputs.rf, inputs.booster].forEach((input, i) => {
                const keys = ["ff", "rf", "booster"];
                input.classList.add("is-invalid");
                const errorEl = createOrGetErrorElement(input, keys[i]);
                errorEl.textContent = msg;
            });
        } else {
            ["ff", "rf", "booster"].forEach(key => clearError(inputs[key], key));
        }
    }

    // Attach input listeners
    if (inputs.passenger) {
        inputs.passenger.addEventListener("input", () =>
            validateSingleField(inputs.passenger, "passenger", limits.passengers, "Passenger count")
        );
    }

    if (inputs.luggage) {
        inputs.luggage.addEventListener("input", () =>
            validateSingleField(inputs.luggage, "luggage", limits.luggage, "Luggage count")
        );
    }

    [["ff", "Forward-facing car seats"], ["rf", "Rear-facing car seats"], ["booster", "Booster seats"]].forEach(
        ([key, label]) => {
            if (inputs[key]) {
                inputs[key].addEventListener("input", () => {
                    validateSingleField(inputs[key], key, limits[key], label);
                    validateTotalCarseats();
                });
            }
        }
    );
});