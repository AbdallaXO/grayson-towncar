def validate_vehicle_constraints(vehicle, cleaned_data, add_error):
    """Validate passenger, luggage, and car seat counts against
    the chosen vehicle's capacity — accounting for paid extras."""

    passenger_count = cleaned_data.get("passenger_count")
    luggage_count = cleaned_data.get("luggage_count")
    ff_carseats = cleaned_data.get("ff_carseats") or 0
    rf_carseats = cleaned_data.get("rf_carseats") or 0
    boosters = cleaned_data.get("booster_seats") or 0
    extra_carseats = cleaned_data.get("extra_carseats") or 0
    extra_boosters = cleaned_data.get("extra_boosters") or 0

    if passenger_count and passenger_count > vehicle.capacity:
        add_error(
            "passenger_count",
            f"{vehicle} can accommodate up to {vehicle.capacity} passengers. Please select a different vehicle to fit your group.",
        )

    if luggage_count and luggage_count > vehicle.luggage_capacity:
        add_error(
            "luggage_count",
            f"{vehicle} has space for up to {vehicle.luggage_capacity} suitcases.\nNote: Small carry-ons and backpacks do **not** count as suitcases.",
        )

    # Per-type physical limits (hard caps — extras don't increase these)
    if ff_carseats > vehicle.ff_carseats_max:
        add_error(
            "ff_carseats",
            f"{vehicle} allows a maximum of {vehicle.ff_carseats_max} forward-facing car seat(s).",
        )

    if rf_carseats > vehicle.rf_carseats_max:
        add_error(
            "rf_carseats",
            f"{vehicle} allows a maximum of {vehicle.rf_carseats_max} rear-facing car seat(s).",
        )

    if boosters > vehicle.boosters_max:
        add_error(
            "booster_seats",
            f"{vehicle} allows a maximum of {vehicle.boosters_max} booster seat(s).",
        )

    # Total physical capacity (hard cap)
    total_seats = ff_carseats + rf_carseats + boosters
    if total_seats > vehicle.carseats_capacity:
        add_error(
            "need_carseats",
            f"{vehicle} can accommodate up to {vehicle.carseats_capacity} car seats and boosters combined.",
        )

    # Combined included limits (extras raise this threshold via payment)
    max_cs = vehicle.included_carseats + extra_carseats
    max_bs = vehicle.included_boosters + extra_boosters
    if ff_carseats + rf_carseats > max_cs:
        add_error(
            "need_carseats",
            f"{vehicle} allows only {max_cs} car seat(s) total (rear-facing and forward-facing combined).",
        )
    if boosters > max_bs:
        add_error(
            "booster_seats",
            f"{vehicle} allows only {max_bs} booster seat(s).",
        )
