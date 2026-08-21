CARSEAT_CHOICES = [
    ("booster", "Booster Seat"),
    ("rear_facing", "Rear-Facing Car Seat"),
    ("forward_facing", "Forward-Facing Car Seat"),
]
TRIP_CHOICES = [
    ("one_way", "One Way"),
    ("round_trip", "Round Trip"),
]
FLIGHT_TYPE_CHOICES = [
    ("arrival", "Arrival"),
    ("departure", "Departure"),
]
RESERVTION_STATUS = [
    ("pending", "Pending"),
    ("cancelled", "Cancelled"),
    ("confirmed", "Confirmed"),
    ("completed", "Completed"),
]

DRIVER_STATUS = [
    ("in-progress", "In-Progress"),
    ("confirmed", "Confirmed"),
    ("on-the-way", "On the Way"),
    ("on-location", "On-Location"),
    ("picked-up", "Picked-Up"),
    ("completed", "Completed"),
    ("cancelled", "Cancelled"),
]

# --- Where a leg is in its own run, as sets rather than strings ---------------
# One definition, because two surfaces already have to agree about it: the live
# ETA sweep (dispatching/samsara_risk.py) decides what to measure the car
# against, and the right-click "Route to..." menu (dispatching/vehicle_routing.py)
# decides where to send it. When they disagreed, the board badge read "18 min to
# drop-off" while the menu underneath it still offered a route to the pickup.
#
# ON_TRIP: the chauffeur has the guest, or is standing at the door about to —
# either way the pickup is behind him and the next stop is the DROP-OFF.
ON_TRIP_STATUSES = {"picked-up", "on-location"}
# CLOSED: the leg is off the road. There is no next stop at all.
CLOSED_STATUSES = {"completed", "cancelled"}
