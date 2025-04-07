from django.core.exceptions import ValidationError

def validate_carseat_limits(vehicle, rear, forward, booster):
    total_carseats = rear + forward
    limits = {
        "towncar": {"carseats": 1, "boosters": 1},
        "suv": {"carseats": 2, "boosters": 2},
        "van": {"carseats": 2, "boosters": 2},
    }
    name = vehicle.name.lower()
    limit = limits.get(name, {"carseats": 0, "boosters": 0})

    if total_carseats > limit["carseats"]:
        raise ValidationError(f"{vehicle.name} allows up to {limit['carseats']} total car seats.")
    if booster > limit["boosters"]:
        raise ValidationError(f"{vehicle.name} allows up to {limit['boosters']} booster seats.")
