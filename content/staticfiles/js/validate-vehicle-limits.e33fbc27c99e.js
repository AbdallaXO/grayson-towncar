// Vehicle Capacity Validation Script (with extra seat upsell)

document.addEventListener("DOMContentLoaded", function () {
    const limitsEl = document.getElementById("vehicle-limits");
    if (!limitsEl) return;

    const limits = {
        vehicleType: limitsEl.dataset.vehicleType || "",
        passengers: parseInt(limitsEl.dataset.maxPassengers),
        luggage: parseInt(limitsEl.dataset.maxLuggage),
        ff: parseInt(limitsEl.dataset.maxFf),
        rf: parseInt(limitsEl.dataset.maxRf),
        booster: parseInt(limitsEl.dataset.maxBoosters),
        carseats: parseInt(limitsEl.dataset.maxCarseats),
        includedCarseats: parseInt(limitsEl.dataset.includedCarseats),
        includedBoosters: parseInt(limitsEl.dataset.includedBoosters),
    };

    // Combined limits from Django admin (how many are included before extra fees)
    const baseCombinedCs = limits.includedCarseats;
    const baseCombinedBs = limits.includedBoosters;

    const fees = {
        carseat: parseFloat(limitsEl.dataset.extraCarseatFee) || 30,
        booster: parseFloat(limitsEl.dataset.extraBoosterFee) || 30,
    };

    const inputs = {
        passenger: document.getElementById("id_passenger_count"),
        luggage: document.getElementById("id_luggage_count"),
        ff: document.getElementById("id_ff_carseats"),
        rf: document.getElementById("id_rf_carseats"),
        booster: document.getElementById("id_booster_seats"),
    };

    // Hidden fields for extra seat counts
    const extraFields = {
        carseats: document.getElementById("id_extra_carseats"),
        boosters: document.getElementById("id_extra_boosters"),
    };

    // Track how many extras were approved
    let extraCarseats = parseInt(extraFields.carseats?.value || 0);
    let extraBoosters = parseInt(extraFields.boosters?.value || 0);

    const errors = {};

    // -- Modal setup --
    const modalEl = document.getElementById("extraSeatModal");
    const modal = modalEl ? new bootstrap.Modal(modalEl) : null;
    const confirmBtn = document.getElementById("extraSeatConfirm");
    const feeDisplay = document.getElementById("extraSeatFee");
    const messageEl = document.getElementById("extraSeatMessage");
    const typePicker = document.getElementById("extraSeatTypePicker");
    const footerSingle = document.getElementById("extraSeatFooterSingle");
    const footerPicker = document.getElementById("extraSeatFooterPicker");

    // Pending action when user clicks confirm
    let pendingAction = null;

    function afterExtraSeatAdded() {
        if (modal) modal.hide();
        syncInputMaxAttrs();
        updateExtraSeatDisplay();
        validateCarseatCombo();
        validateTotalCarseats();
        updateUpsellBanners();
    }

    if (confirmBtn) {
        confirmBtn.addEventListener("click", function () {
            if (pendingAction) {
                pendingAction();
                pendingAction = null;
            }
            afterExtraSeatAdded();
        });
    }

    // Seat type picker buttons (rear-facing / forward-facing choice)
    document.querySelectorAll(".extra-seat-type-btn").forEach(function (btn) {
        btn.addEventListener("click", function () {
            const seatType = this.dataset.seatType;
            const targetInput = seatType === "rf" ? inputs.rf : inputs.ff;
            if (targetInput && hasRoomFor(targetInput === inputs.rf ? "id_rf_carseats" : "id_ff_carseats")) {
                addExtraSeat("carseat", targetInput);
            }
            afterExtraSeatAdded();
        });
    });

    // Show/hide picker vs single confirm
    function setModalMode(mode) {
        if (typePicker) typePicker.classList.toggle("d-none", mode !== "pick");
        if (footerSingle) footerSingle.classList.toggle("d-none", mode === "pick");
        if (footerPicker) footerPicker.classList.toggle("d-none", mode !== "pick");
    }

    // -- Error helpers --
    function createOrGetErrorElement(input, key) {
        if (errors[key]) return errors[key];
        const errorEl = document.createElement("div");
        errorEl.classList.add("text-danger", "small", "mt-1");
        errorEl.id = `error-${key}`;
        const stepper = input.closest('.gt-stepper');
        const container = stepper ? stepper.parentNode : input.parentNode;
        container.appendChild(errorEl);
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

    // -- Effective included limits (base included + paid extras) --
    // These grow as the customer pays for extras, but are capped at physical limits.
    function effectiveCombinedCs() { return Math.min(baseCombinedCs + extraCarseats, limits.ff + limits.rf); }
    function effectiveCombinedBs() { return Math.min(baseCombinedBs + extraBoosters, limits.booster); }

    // Helper: can we physically fit one more car seat of this type?
    function hasRoomFor(inputId) {
        const ff = parseInt(inputs.ff?.value || 0);
        const rf = parseInt(inputs.rf?.value || 0);
        const booster = parseInt(inputs.booster?.value || 0);
        const total = ff + rf + booster;
        if (total >= limits.carseats) return false; // total capacity reached
        if (inputId === "id_ff_carseats") return ff < limits.ff;
        if (inputId === "id_rf_carseats") return rf < limits.rf;
        if (inputId === "id_booster_seats") return booster < limits.booster;
        return false;
    }

    // -- Validation --
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
                if (input) {
                    input.classList.add("is-invalid");
                    const errorEl = createOrGetErrorElement(input, keys[i]);
                    errorEl.textContent = msg;
                }
            });
        } else {
            ["ff", "rf", "booster"].forEach(key => {
                if (inputs[key]) clearError(inputs[key], key);
            });
        }
    }

    function validateCarseatCombo() {
        const ff = parseInt(inputs.ff?.value || 0);
        const rf = parseInt(inputs.rf?.value || 0);
        const booster = parseInt(inputs.booster?.value || 0);
        const maxCs = effectiveCombinedCs();
        const maxBs = effectiveCombinedBs();

        if (ff + rf > maxCs) {
            const msg = `Only ${maxCs} car seat(s) total allowed (rear-facing and forward-facing combined).`;
            [inputs.ff, inputs.rf].forEach((input, i) => {
                const keys = ["ff", "rf"];
                if (input) {
                    input.classList.add("is-invalid");
                    const errorEl = createOrGetErrorElement(input, keys[i]);
                    errorEl.textContent = msg;
                }
            });
        } else {
            ["ff", "rf"].forEach(key => {
                if (inputs[key]) clearError(inputs[key], key);
            });
        }

        if (booster > maxBs) {
            showError(inputs.booster, "booster", `Only ${maxBs} booster seat(s) allowed.`);
        }
    }

    // -- Extra seat upsell logic --
    function addExtraSeat(seatType, input) {
        if (!input) return;
        // Enforce physical per-type limit
        const current = parseInt(input.value || 0);
        const typeMax = input === inputs.ff ? limits.ff :
                        input === inputs.rf ? limits.rf : limits.booster;
        if (current >= typeMax) return;

        // Enforce total physical capacity
        const ff = parseInt(inputs.ff?.value || 0);
        const rf = parseInt(inputs.rf?.value || 0);
        const booster = parseInt(inputs.booster?.value || 0);
        if (ff + rf + booster >= limits.carseats) return;

        const isBooster = seatType === "booster";
        if (isBooster) {
            extraBoosters++;
            if (extraFields.boosters) extraFields.boosters.value = extraBoosters;
        } else {
            extraCarseats++;
            if (extraFields.carseats) extraFields.carseats.value = extraCarseats;
        }
        input.value = current + 1;
        input.dispatchEvent(new Event("change", { bubbles: true }));
        input.dispatchEvent(new Event("input", { bubbles: true }));
        updateExtraSeatDisplay();
        validateCarseatCombo();
        validateTotalCarseats();
        updateUpsellBanners();
    }

    // Show modal for extra seat confirmation
    function promptExtraSeat(seatType, input, mode) {
        if (!modal) return false;

        const isBooster = seatType === "booster";
        const fee = isBooster ? fees.booster : fees.carseat;
        const label = isBooster ? "booster seat" : "car seat";

        if (feeDisplay) feeDisplay.textContent = "$" + fee.toFixed(0);
        if (messageEl) {
            messageEl.innerHTML = `Would you like to add an additional <strong>${label}</strong> for <strong>$${fee.toFixed(0)}</strong>?`;
        }

        const usePicker = !isBooster && mode === "pick";
        setModalMode(usePicker ? "pick" : "confirm");

        if (!usePicker) {
            pendingAction = function () {
                addExtraSeat(seatType, input);
            };
        }

        modal.show();
        return true;
    }

    // -- Inline upsell banners (shown when at included limit AND physical room remains) --
    function updateUpsellBanners() {
        const panel = document.querySelector(".gt-carseat-panel");
        if (!panel) return;

        const ff = parseInt(inputs.ff?.value || 0);
        const rf = parseInt(inputs.rf?.value || 0);
        const booster = parseInt(inputs.booster?.value || 0);
        const total = ff + rf + booster;
        const atTotalCap = total >= limits.carseats;

        // Car seat banner — show when at included limit but still have physical room
        const csAtIncluded = ff + rf >= effectiveCombinedCs();
        const csHasRoom = !atTotalCap && (ff < limits.ff || rf < limits.rf);
        let csBanner = document.getElementById("gt-upsell-carseat");
        if (csAtIncluded && csHasRoom) {
            if (!csBanner) {
                csBanner = document.createElement("div");
                csBanner.id = "gt-upsell-carseat";
                csBanner.className = "gt-extra-seat-banner";
                csBanner.innerHTML = `<span class="gt-esb-icon"><i class="bi bi-plus-lg" aria-hidden="true"></i></span>
                    <span class="gt-esb-text">Need more car seats? <strong>Add an extra</strong> for your family</span>
                    <span class="gt-esb-price">+$${fees.carseat.toFixed(0)}</span>`;
                const rows = panel.querySelector(".gt-carseat-rows");
                if (rows) rows.after(csBanner);
            }
            csBanner.onclick = function () {
                promptExtraSeat("carseat", null, "pick");
            };
        } else if (csBanner) {
            csBanner.remove();
        }

        // Booster banner — show when at included limit but still have physical room
        const bsAtIncluded = booster >= effectiveCombinedBs();
        const bsHasRoom = !atTotalCap && booster < limits.booster;
        let bsBanner = document.getElementById("gt-upsell-booster");
        if (bsAtIncluded && bsHasRoom) {
            if (!bsBanner) {
                bsBanner = document.createElement("div");
                bsBanner.id = "gt-upsell-booster";
                bsBanner.className = "gt-extra-seat-banner";
                bsBanner.innerHTML = `<span class="gt-esb-icon"><i class="bi bi-plus-lg" aria-hidden="true"></i></span>
                    <span class="gt-esb-text">Need more boosters? <strong>Add an extra</strong> for your family</span>
                    <span class="gt-esb-price">+$${fees.booster.toFixed(0)}</span>`;
                const rows = panel.querySelector(".gt-carseat-rows");
                if (rows) rows.after(bsBanner);
            }
            bsBanner.onclick = function () {
                promptExtraSeat("booster", inputs.booster);
            };
        } else if (bsBanner) {
            bsBanner.remove();
        }
    }

    // Expose for stepper integration
    window._extraSeatCheck = function (inputId, action) {
        if (action !== "increment") return false;

        const input = document.getElementById(inputId);
        if (!input) return false;

        // Hard physical limit — show a message and block
        if (!hasRoomFor(inputId)) {
            const ff = parseInt(inputs.ff?.value || 0);
            const rf = parseInt(inputs.rf?.value || 0);
            const booster = parseInt(inputs.booster?.value || 0);
            const key = inputId === "id_ff_carseats" ? "ff" :
                        inputId === "id_rf_carseats" ? "rf" : "booster";
            if (ff + rf + booster >= limits.carseats) {
                showError(input, key, `Maximum capacity reached — this vehicle fits ${limits.carseats} child seat(s) total.`);
            } else {
                const typeLabel = key === "ff" ? "forward-facing" : key === "rf" ? "rear-facing" : "booster";
                const typeMax = key === "booster" ? limits.booster : limits[key];
                showError(input, key, `Maximum ${typeLabel} seats for this vehicle (${typeMax}).`);
            }
            return true; // block the increment
        }

        if (inputId === "id_ff_carseats" || inputId === "id_rf_carseats") {
            const ff = parseInt(inputs.ff?.value || 0);
            const rf = parseInt(inputs.rf?.value || 0);
            if (ff + rf >= effectiveCombinedCs()) {
                return promptExtraSeat("carseat", input);
            }
        } else if (inputId === "id_booster_seats") {
            const currentVal = parseInt(input.value || 0);
            if (currentVal >= effectiveCombinedBs()) {
                return promptExtraSeat("booster", input);
            }
        }

        return false;
    };

    // -- Set max attributes on inputs (physical per-type limits — never grow) --
    function syncInputMaxAttrs() {
        if (inputs.passenger) inputs.passenger.setAttribute("max", limits.passengers);
        if (inputs.luggage) inputs.luggage.setAttribute("max", limits.luggage);

        if (inputs.ff) { inputs.ff.setAttribute("min", 0); inputs.ff.setAttribute("max", limits.ff); }
        if (inputs.rf) { inputs.rf.setAttribute("min", 0); inputs.rf.setAttribute("max", limits.rf); }
        if (inputs.booster) { inputs.booster.setAttribute("min", 0); inputs.booster.setAttribute("max", limits.booster); }

        [inputs.ff, inputs.rf, inputs.booster, inputs.passenger, inputs.luggage].forEach(function (input) {
            if (input) input.dispatchEvent(new Event("stepper-sync"));
        });
    }

    syncInputMaxAttrs();

    // -- Extra seat price display --
    function updateExtraSeatDisplay() {
        const totalExtraFee = (extraCarseats * fees.carseat) + (extraBoosters * fees.booster);

        let display = document.getElementById("extra-seat-fee-display");
        if (totalExtraFee > 0) {
            if (!display) {
                display = document.createElement("div");
                display.id = "extra-seat-fee-display";
                display.className = "gt-info-callout warning mt-3";
                const panel = document.querySelector(".gt-carseat-panel");
                if (panel) panel.appendChild(display);
            }

            const items = [];
            if (extraCarseats > 0) {
                items.push(`${extraCarseats} extra car seat${extraCarseats > 1 ? "s" : ""} ($${(extraCarseats * fees.carseat).toFixed(0)})`);
            }
            if (extraBoosters > 0) {
                items.push(`${extraBoosters} extra booster${extraBoosters > 1 ? "s" : ""} ($${(extraBoosters * fees.booster).toFixed(0)})`);
            }

            display.innerHTML = `<i class="bi bi-plus-circle" aria-hidden="true"></i> <span><strong>Extra seats added:</strong> ${items.join(", ")} &mdash; <strong>+$${totalExtraFee.toFixed(0)}</strong> added to your total.</span>`;
        } else if (display) {
            display.remove();
        }
    }

    // -- Recalculate extras when seats are removed --
    function recalcExtras() {
        const ff = parseInt(inputs.ff?.value || 0);
        const rf = parseInt(inputs.rf?.value || 0);
        const booster = parseInt(inputs.booster?.value || 0);

        const neededExtraCs = Math.max(0, (ff + rf) - baseCombinedCs);
        const neededExtraBs = Math.max(0, booster - baseCombinedBs);

        if (neededExtraCs !== extraCarseats || neededExtraBs !== extraBoosters) {
            extraCarseats = neededExtraCs;
            extraBoosters = neededExtraBs;
            if (extraFields.carseats) extraFields.carseats.value = extraCarseats;
            if (extraFields.boosters) extraFields.boosters.value = extraBoosters;
            syncInputMaxAttrs();
            updateExtraSeatDisplay();
        }
        updateUpsellBanners();
    }

    // -- Attach input listeners --
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

    [["ff", "Forward-facing car seats", limits.ff],
     ["rf", "Rear-facing car seats", limits.rf],
     ["booster", "Booster seats", limits.booster]].forEach(
        ([key, label, max]) => {
            if (inputs[key]) {
                inputs[key].addEventListener("input", () => {
                    validateSingleField(inputs[key], key, max, label);
                    validateTotalCarseats();
                    validateCarseatCombo();
                    recalcExtras();
                });
            }
        }
    );

    // Initialize display on page load
    updateExtraSeatDisplay();
    updateUpsellBanners();
});
