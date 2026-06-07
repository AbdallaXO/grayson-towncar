"""
TEST-VALIDATION DATA — LOCAL DB ONLY. Do NOT run against prod.

Corrects affiliate Waleed (aka Oualid, Driver id 7) DriverPayRate rows in the local scrubbed
SQLite copy so the Farm-Out Opportunity-Cost Optimizer can be validated against known rates
(see C:\\Users\\14078\\.claude\\plans\\continuing-the-farm-out-opportunity-cost-serene-hartmanis.md, Step 1).

Founder's real rates for Waleed (same price for ALL vehicle classes he covers -> vehicle=NULL):
  * All local jobs from MCO (Disney, Universal, resorts, etc.): $70
  * Port Canaveral routes: $125   (he was mispriced at $120 locally)
  * Sanford (SFB) routes:  $125   (he was mispriced at $120 locally)

This script ONLY writes DriverPayRate rows. It NEVER creates Route rows. It is idempotent and
re-runnable. Run with:  .venv/Scripts/python.exe scratch/seed_waleed_rates.py
"""
import os
import sys
from decimal import Decimal

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "business.settings")
django.setup()

from drivers.models import Driver, DriverPayRate  # noqa: E402
from drivers.pay_calc import _find_rate  # noqa: E402
from rates.models import Location, Route  # noqa: E402
from django.db.models import Q  # noqa: E402

WALEED_ID = 7
LOCAL_RATE = Decimal("70.00")
PORT_SANFORD_RATE = Decimal("125.00")

# Routes confirmed to exist in the local DB (verified before writing). We target by route id so a
# stray same-priced row elsewhere is never swept up. Each tuple: (route_id, target_rate, label).
PORT_SANFORD_ROUTES = [
    (11, PORT_SANFORD_RATE, "Disney -> Port Canaveral"),
    (12, PORT_SANFORD_RATE, "Port Canaveral -> MCO"),
    (13, PORT_SANFORD_RATE, "Sanford -> Disney"),
    (14, PORT_SANFORD_RATE, "Sanford -> Universal"),
]
# Local MCO routes Waleed was missing a rate for (all confirmed present in the Route table).
MISSING_LOCAL_ROUTES = [
    (4, LOCAL_RATE, "MCO -> Disney Springs Hotels"),
    (16, LOCAL_RATE, "MCO -> Winter Garden Resorts"),
    (19, LOCAL_RATE, "MCO -> Flamingo Crossings"),
]


def upsert_both_null_rate(driver, route, base_pay, label):
    """Upsert the (driver, route, vehicle=NULL, direction='both') rate row. SQLite unique indexes
    treat NULLs as distinct, so update_or_create on vehicle=None can silently DUPLICATE — guard by
    asserting at most one NULL-vehicle 'both' row first, then update-or-create explicitly."""
    qs = DriverPayRate.objects.filter(driver=driver, route=route, vehicle__isnull=True,
                                      direction="both")
    n = qs.count()
    if n > 1:
        raise SystemExit(
            f"ABORT: route#{route.id} ({label}) has {n} NULL-vehicle 'both' rows for Waleed — "
            f"ambiguous; resolve the duplicates by hand before seeding.")
    if n == 1:
        row = qs.first()
        old = row.base_pay
        if old == base_pay:
            print(f"  route#{route.id:<3} {label:<32} ${old}  (unchanged)")
        else:
            row.base_pay = base_pay
            row.save(update_fields=["base_pay"])
            print(f"  route#{route.id:<3} {label:<32} ${old} -> ${base_pay}")
    else:
        DriverPayRate.objects.create(driver=driver, route=route, vehicle=None,
                                     direction="both", base_pay=base_pay)
        print(f"  route#{route.id:<3} {label:<32} (no row) -> ${base_pay}  [CREATED]")


def main():
    w = Driver.objects.filter(id=WALEED_ID, driver_type="affiliate").first()
    if w is None:
        raise SystemExit(f"ABORT: no affiliate Driver at id {WALEED_ID}")
    print(f"WALEED = {w.id} {w} ({w.driver_type})\n")

    print("== Port / Sanford routes -> $125 (was $120) ==")
    for rid, rate, label in PORT_SANFORD_ROUTES:
        rt = Route.objects.filter(id=rid).first()
        if rt is None:
            print(f"  route#{rid} ({label}): NOT FOUND — skipped (no Route created).")
            continue
        upsert_both_null_rate(w, rt, rate, label)

    print("\n== Missing local MCO routes -> $70 ==")
    for rid, rate, label in MISSING_LOCAL_ROUTES:
        rt = Route.objects.filter(id=rid).first()
        if rt is None:
            print(f"  route#{rid} ({label}): NOT FOUND — reported, no Route created.")
            continue
        upsert_both_null_rate(w, rt, rate, label)

    # ── Verify via the EXACT runtime lookup (direction-aware) for both directions ──
    print("\n== Verify via pay_calc._find_rate (both directions) ==")
    checks = [
        (2, LOCAL_RATE, "MCO <-> Disney (local)"),
        (4, LOCAL_RATE, "MCO <-> Disney Springs (new)"),
        (16, LOCAL_RATE, "MCO <-> Winter Garden (new)"),
        (19, LOCAL_RATE, "MCO <-> Flamingo (new)"),
        (12, PORT_SANFORD_RATE, "Port Canaveral <-> MCO"),
        (11, PORT_SANFORD_RATE, "Disney <-> Port Canaveral"),
        (13, PORT_SANFORD_RATE, "Sanford <-> Disney"),
        (14, PORT_SANFORD_RATE, "Sanford <-> Universal"),
    ]
    ok = True
    for rid, expected, label in checks:
        rt = Route.objects.filter(id=rid).first()
        if rt is None:
            print(f"  route#{rid} ({label}): NOT FOUND")
            ok = False
            continue
        fwd = _find_rate(w, rt, None, "forward")
        rev = _find_rate(w, rt, None, "reverse")
        flag = "OK" if (fwd == expected and rev == expected) else "MISMATCH"
        if flag == "MISMATCH":
            ok = False
        print(f"  [{flag}] route#{rid:<3} {label:<30} fwd=${fwd} rev=${rev} (expected ${expected})")

    # ── Report the routes we deliberately did NOT touch (no invention) ──
    print("\n== Reported (NOT modified — confirm with founder) ==")
    print("  * No literal MCO<->Sanford(SFB) route exists in the local DB (airport-to-airport);")
    print("    Sanford rates live on Sanford<->Disney (#13) and Sanford<->Universal (#14).")
    mco = Location.objects.filter(name__icontains="Orlando International").first()
    canaveral = Location.objects.filter(name__icontains="Canaveral").first()
    if canaveral:
        carded = set(DriverPayRate.objects.filter(driver=w).values_list("route_id", flat=True))
        for rt in Route.objects.filter(Q(origin_id=canaveral.id) | Q(destination_id=canaveral.id)):
            if rt.id not in carded:
                print(f"  * Port route#{rt.id} [{rt.origin.name} -> {rt.destination.name}] "
                      f"is NOT carded by Waleed (legs on it are uncomputable for him).")

    print("\nDONE." + ("" if ok else "  *** SOME VERIFICATIONS FAILED — review above. ***"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
