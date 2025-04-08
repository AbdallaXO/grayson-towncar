def validate_vehicle_constraints(vehicle, cleaned_data, add_error):
    """This Checks for each reservation form
    Passenger Count Against the chosen Vehicles Passenger Capacity
    Luggage Count Against the chosen vehicles luggage capacity
    checks the carseats against the vehicles carseats,
    for e.g if a tonwcar only has 1 carseat, a guest fills in 2 carseats
    returns a field error displaying the towncar cant have more than N carseats """

    
    passenger_count = cleaned_data.get("passenger_count")
    luggage_count = cleaned_data.get("luggage_count")
    ff_carseats = cleaned_data.get("ff_carseats") or 0
    rf_carseats = cleaned_data.get("rf_carseats") or 0
    boosters = cleaned_data.get("booster_seats") or 0

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
            "boosters",
            f"{vehicle} allows a maximum of {vehicle.boosters_max} booster seat(s).",
        )

    if ff_carseats + rf_carseats + boosters > vehicle.carseats_capacity:
        add_error(
            "need_carseats",
            f"{vehicle} can accommodate a total of up to {vehicle.carseats_capacity} car seats and boosters combined.",
        )
