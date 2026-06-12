# Diff auto-assign vs manual-tweaks schedules and score against founder rules
import csv
from collections import defaultdict

BASE = r"c:\Users\admin\OneDrive\Desktop\grayson-towncar\.analysis"
def load(p):
    with open(p, encoding='utf-8', errors='replace') as f:
        return {r['leg_id']: r for r in csv.DictReader(f)}

auto = load(BASE + r"\legs_sunday_after_autoassign.csv")
man = load(BASE + r"\legs_sunday_manual.csv")

print(f"legs: auto={len(auto)} manual={len(man)}; ids match: {set(auto)==set(man)}\n")
print("CHANGES (auto -> manual):")
changes = []
for lid in sorted(auto, key=lambda x: man[x]['pickup_time'] if x in man else ''):
    a, m = auto[lid]['assigned_driver'], man[lid]['assigned_driver']
    if a != m:
        r = man[lid]
        changes.append((lid, a, m, r))
        print(f"  {r['pickup_time']:>8s} {r['trip_type']:<18s} {r['vehicle_type']:<13s} {r['passenger_count']:>2s}pax leg {lid}: {a} -> {m}")
print(f"\ntotal changes: {len(changes)}")

def stats(d, label):
    un = [r for r in d.values() if r['assigned_driver'] == 'Unassigned']
    dep_un = [r for r in un if r['trip_type'] == 'Departure']
    arr_un = [r for r in un if r['trip_type'] in ('Arrival',)]
    cru_un = [r for r in un if 'Cruise' in r['trip_type']]
    v14_un = [r for r in un if r['vehicle_type'].lower() == 'van (14 pax)']
    px = lambda rs: sum(int(r['passenger_count']) for r in rs)
    print(f"\n{label}: unassigned {len(un)} legs / {px(un)} pax")
    print(f"  departures farmed: {len(dep_un)} ({px(dep_un)} pax)  arrivals farmed: {len(arr_un)} ({px(arr_un)} pax)  cruise farmed: {len(cru_un)} ({px(cru_un)} pax)  V14 farmed: {len(v14_un)} ({px(v14_un)} pax)")

stats(auto, "AUTO")
stats(man, "MANUAL")

print("\nPER-DRIVER JOB COUNTS (auto -> manual):")
ca, cm = defaultdict(int), defaultdict(int)
for r in auto.values(): ca[r['assigned_driver']] += 1
for r in man.values(): cm[r['assigned_driver']] += 1
for d in sorted(set(ca) | set(cm)):
    if d != 'Unassigned':
        print(f"  {d:20s} {ca[d]:2d} -> {cm[d]:2d}")
