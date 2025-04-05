function initAutocomplete() {
    const legCount = 2; // since we only support round_trips

    for (let i = 1; i <= legCount; i++) {
        const pickupFieldList = document.getElementsByName(`leg${i}-pickup_location`);
        const dropoffFieldList = document.getElementsByName(`leg${i}-dropoff_location`);

        const pickupField = pickupFieldList[0];
        const dropoffField = dropoffFieldList[0];

        if (pickupField) {
            new google.maps.places.Autocomplete(pickupField, {
                componentRestrictions: { country: ["us"] },
                fields: ["address_components", "geometry"],
                types: ["establishment"]
            });
        }

        if (dropoffField) {
            new google.maps.places.Autocomplete(dropoffField, {
                componentRestrictions: { country: ["us"] },
                fields: ["address_components", "geometry"],
                types: ["establishment"]
            });
        }
    }
}

window.addEventListener("load", function () {
    if (window.google) {
        initAutocomplete();
    }
});
