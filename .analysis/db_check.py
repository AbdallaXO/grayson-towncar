# Read-only DB check for 2026-06-14 schedule review
from datetime import date
target = date(2026, 6, 14)

from drivers.models import Driver, DriverVehicleAssignment
try:
    from drivers.models import FleetVehicle
except ImportError:
    FleetVehicle = None

print("=== FLEET VEHICLES BY TYPE ===")
if FleetVehicle:
    from collections import Counter
    vehicles = list(FleetVehicle.objects.all())
    for v in vehicles:
        print(f"  #{getattr(v,'number',v.pk)} {v.vehicle_type} active={getattr(v,'is_active','?')}")
else:
    print("  FleetVehicle model not found under drivers.models")

print("\n=== DRIVER-VEHICLE PAIRINGS 2026-06-14 ===")
for a in DriverVehicleAssignment.objects.filter(date=target).select_related('driver','vehicle'):
    v = a.vehicle
    vd = f"#{getattr(v,'number',v.pk)} {v.vehicle_type}" if v else "NONE"
    print(f"  {a.driver} -> {vd}")

print("\n=== UNPAIRED ACTIVE VEHICLES 2026-06-14 ===")
if FleetVehicle:
    used = set(DriverVehicleAssignment.objects.filter(date=target).values_list('vehicle_id', flat=True))
    for v in FleetVehicle.objects.exclude(pk__in=used):
        print(f"  #{getattr(v,'number',v.pk)} {v.vehicle_type} active={getattr(v,'is_active','?')}")

print("\n=== EFFECTIVE AVAILABILITY 2026-06-14 ===")
for d in Driver.objects.all():
    try:
        av = d.get_effective_availability(target)
        print(f"  {d}: {av}")
    except Exception as e:
        print(f"  {d}: ERROR {e}")
