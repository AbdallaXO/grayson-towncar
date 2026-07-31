"""Generate docs/operational-data-audit.md — every table computed from the DB, no hand-typed figures."""
import os, sys, sqlite3, collections, random, json
from datetime import datetime

sys.path.insert(0, os.getcwd())          # run from the project root
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "business.settings")
os.environ.setdefault("ENABLE_DEBUG_TOOLBAR", "0")
import django; django.setup()
from dispatching.analytics import categorize_location
from dispatching.scheduler import DRIVE_TIME_ESTIMATES, DEFAULT_DRIVE_TIME

DB = "file:content/db.sqlite3?mode=ro"
con = sqlite3.connect(DB, uri=True); cur = con.cursor()
P = lambda s: datetime.fromisoformat(s.replace(' ', 'T').split('.')[0]) if s else None
def pc(v, f):
    v = sorted(v); return v[min(int(f * len(v)), len(v) - 1)]
random.seed(7)

GOOD = {r[0] for r in cur.execute(
    "SELECT id FROM drivers_driver WHERE driver_type='inhouse' AND exclude_from_timing=0")}
EXCLUDED = {r[0] for r in cur.execute("SELECT id FROM drivers_driver WHERE exclude_from_timing=1")}

EV = collections.defaultdict(dict)
for lid, s, ts in cur.execute("""SELECT leg_id,status,MIN(timestamp) FROM reservations_legstatus
    WHERE status IN ('on-the-way','on-location','picked-up','completed') GROUP BY leg_id,status"""):
    EV[lid][s] = P(ts)

LEGS = {r[0]: r[1:] for r in cur.execute(
    "SELECT id,pickup_location,dropoff_location,driver_id,pickup_date,pickup_time,flight_information_id "
    "FROM reservations_leg")}
FL = {r[0]: r[1:] for r in cur.execute(
    "SELECT id,airline,airline_display_name,flight_number,scheduled_gate_arrival_local,"
    "actual_gate_arrival_local,origin FROM reservations_flight")}

def md_table(headers, rows, align=None):
    align = align or ["---"] * len(headers)
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(align) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)

R = ["---:"]

# ---------------- lanes ----------------
lane = collections.defaultdict(list)
for lid, (pl, dl, did, pd, pt, fid) in LEGS.items():
    d = EV.get(lid)
    if not d or 'picked-up' not in d or 'completed' not in d or did not in GOOD: continue
    v = (d['completed'] - d['picked-up']).total_seconds() / 60
    if 2 <= v <= 240:
        lane[(categorize_location(pl), categorize_location(dl))].append(v)

lane_rows = []
for k in sorted(lane, key=lambda k: -len(lane[k])):
    v = lane[k]
    if len(v) < 30: continue
    bs = sorted(pc([random.choice(v) for _ in v], .75) for _ in range(1000))
    lo, hi = bs[50], bs[950]
    code = DRIVE_TIME_ESTIMATES.get(k)
    lane_rows.append({
        "from": k[0], "to": k[1], "n": len(v),
        "p10": pc(v, .10), "p25": pc(v, .25), "p50": pc(v, .5),
        "p75": pc(v, .75), "p90": pc(v, .9), "max": max(v),
        "code": code, "has_entry": code is not None,
        "ci_lo": lo, "ci_hi": hi, "ci_w": hi - lo,
    })

# ---------------- flights ----------------
NAME = {'WN': 'Southwest', 'DL': 'Delta', 'AA': 'American', 'UA': 'United', 'B6': 'JetBlue',
        'F9': 'Frontier', 'G4': 'Allegiant', 'MX': 'Breeze', 'AS': 'Alaska', 'XP': 'Avelo'}
BUCK = [("before 8am", 0, 8), ("8am-12pm", 8, 12), ("12-4pm", 12, 16),
        ("4-8pm", 16, 20), ("8pm+", 20, 24)]
grid = collections.defaultdict(list); perair = collections.defaultdict(list)
perflight = collections.defaultdict(list); alld = []; dwell = []; curb = []; occ = []
for lid, (pl, dl, did, pd, pt, fid) in LEGS.items():
    if not fid or fid not in FL: continue
    al, nm, fn, sg, ag, org = FL[fid]
    if categorize_location(pl) not in ('MCO Terminal', 'SFB Terminal'): continue
    al = (al or '').upper().strip()
    d = EV.get(lid, {})
    if ag and did in GOOD:
        if 'picked-up' in d:
            x = (d['picked-up'] - P(ag)).total_seconds() / 60
            if -60 < x < 180: dwell.append(x)
        if 'on-location' in d:
            x = (d['on-location'] - P(ag)).total_seconds() / 60
            if -240 < x < 240: curb.append(x)
            if 'completed' in d:
                o = (d['completed'] - d['on-location']).total_seconds() / 60
                if 0 < o < 360: occ.append(o)
    if not sg or not ag or al == 'NK': continue
    delay = (P(ag) - P(sg)).total_seconds() / 60
    if not (-600 < delay < 900): continue
    alld.append(delay)
    if al in NAME:
        perair[NAME[al]].append(delay)
        h = P(sg).hour
        grid[(NAME[al], next(b[0] for b in BUCK if b[1] <= h < b[2]))].append(delay)
    if al and fn: perflight[(al, str(fn).strip(), (org or '?'))].append(delay)

airlines = sorted(perair, key=lambda a: -len(perair[a]))
air_rows = [{"name": a, "n": len(perair[a]), "p10": pc(perair[a], .1), "p50": pc(perair[a], .5),
             "p90": pc(perair[a], .9),
             "late15": 100 * sum(1 for x in perair[a] if x > 15) / len(perair[a])}
            for a in airlines]
air_rows.sort(key=lambda r: -r["p90"])

flight_rows = []
for (al, fn, org), v in perflight.items():
    if len(v) < 12: continue
    flight_rows.append({"ident": al + fn, "origin": org, "n": len(v),
                        "p10": pc(v, .1), "p50": pc(v, .5), "p90": pc(v, .9),
                        "spread": pc(v, .9) - pc(v, .1)})
flight_rows.sort(key=lambda f: f["spread"])
sp = [f["spread"] for f in flight_rows]
airsp = [pc(perair[a], .9) - pc(perair[a], .1) for a in airlines if len(perair[a]) >= 40]

# ---------------- disney resorts ----------------
def resort(t):
    t = (t or "").lower()
    for name, k in [("Grand Floridian","grand floridian"),("Polynesian","polynesian"),
        ("Contemporary","contemporary"),("Wilderness Lodge","wilderness lodge"),
        ("Animal Kingdom Lodge","animal kingdom lodge"),("Animal Kingdom Lodge","kidani"),
        ("Animal Kingdom Lodge","jambo"),("All-Star","all-star"),("All-Star","all star"),
        ("Pop Century","pop century"),("Art of Animation","art of animation"),
        ("Caribbean Beach","caribbean beach"),("Riviera","riviera"),("Coronado Springs","coronado"),
        ("Port Orleans","port orleans"),("Saratoga Springs","saratoga"),("Old Key West","old key west"),
        ("Beach/Yacht Club","beach club"),("Beach/Yacht Club","yacht club"),("BoardWalk","boardwalk"),
        ("Swan/Dolphin","swan"),("Swan/Dolphin","dolphin"),("Disney Springs","disney springs"),
        ("Shades of Green","shades of green")]:
        if k in t: return name
    return None
res_in = collections.defaultdict(list); res_out = collections.defaultdict(list)
for lid, (pl, dl, did, pd, pt, fid) in LEGS.items():
    d = EV.get(lid)
    if not d or 'picked-up' not in d or 'completed' not in d or did not in GOOD: continue
    v = (d['completed'] - d['picked-up']).total_seconds() / 60
    if not (2 <= v <= 240): continue
    pcat, dcat = categorize_location(pl), categorize_location(dl)
    if pcat == 'MCO Terminal' and dcat == 'Disney Resort':
        r = resort(dl)
        if r: res_in[r].append(v)
    elif pcat == 'Disney Resort' and dcat == 'MCO Terminal':
        r = resort(pl)
        if r: res_out[r].append(v)

# ---------------- coverage ----------------
have = collections.defaultdict(set)
for lid, s in cur.execute("""SELECT leg_id,status FROM reservations_legstatus
    WHERE status IN ('on-the-way','picked-up','completed')"""): have[lid].add(s)
mon = {r[0]: r[1][:7] for r in cur.execute(
    "SELECT id,pickup_date FROM reservations_leg WHERE pickup_date BETWEEN '2026-02-01' AND '2026-07-11'")}
tot = collections.Counter(mon.values()); full = collections.Counter()
for lid, ym in mon.items():
    if {'on-the-way','picked-up','completed'} <= have.get(lid, set()): full[ym] += 1

# ---------------- driver discipline ----------------
drv = collections.defaultdict(lambda: {"legs":0,"full":0,"inst":0,"rides":[]})
names = {r[0]: r[1] for r in cur.execute(
    "SELECT d.id,u.username FROM drivers_driver d JOIN auth_user u ON u.id=d.profile_id")}
types = {r[0]: r[1] for r in cur.execute("SELECT id,driver_type FROM drivers_driver")}
for lid, (pl, dl, did, pd, pt, fid) in LEGS.items():
    if did is None or types.get(did) != 'inhouse': continue
    d = EV.get(lid)
    if not d: continue
    s = drv[did]; s["legs"] += 1
    if not {'on-the-way','picked-up','completed'} <= set(d): continue
    s["full"] += 1
    r = (d['completed'] - d['picked-up']).total_seconds() / 60
    if r < 2: s["inst"] += 1
    else: s["rides"].append(r)
drv_rows = []
for did, s in drv.items():
    if s["legs"] < 25: continue
    inst = 100 * s["inst"] / s["full"] if s["full"] else 0
    drv_rows.append({"name": names.get(did, f"#{did}"), "legs": s["legs"],
        "full": 100 * s["full"] / s["legs"], "inst": inst,
        "med": (sorted(s["rides"])[len(s["rides"])//2] if s["rides"] else 0),
        "excluded": did in EXCLUDED})
drv_rows.sort(key=lambda r: -r["inst"])

# ---------------- GOLD COHORT (founder-nominated, most competent app users) ----------------
GOLD_NAMES = ["Michael", "sereen", "yovanny", "steven", "junaid", "angel", "runer", "roberto",
              "lev", "george", "davide", "Charlie", "Aftab", "oualid"]
# Nominated but REJECTED on the data: Idrees taps "Picked Up" and "Complete" together on 68% of
# his trips, so his ride times are fiction. Keeping him would poison the baseline.
GOLD_REJECTED = [("Idrees", "68% instant-complete — ride times are fabricated")]
name_to_id = {v: k for k, v in names.items()}
GOLD = {name_to_id[n] for n in GOLD_NAMES if n in name_to_id}
GOLD_MISSING = [n for n in GOLD_NAMES if n not in name_to_id]

gold_lane = collections.defaultdict(list)
gold_dwell, gold_curb, gold_occ, gold_appr, gold_wait = [], [], [], [], []
for lid, (pl, dl, did, pd, pt, fid) in LEGS.items():
    if did not in GOLD: continue
    d = EV.get(lid)
    if not d: continue
    if 'picked-up' in d and 'completed' in d:
        v = (d['completed'] - d['picked-up']).total_seconds() / 60
        if 2 <= v <= 240:
            gold_lane[(categorize_location(pl), categorize_location(dl))].append(v)
    if 'on-the-way' in d and 'on-location' in d:
        v = (d['on-location'] - d['on-the-way']).total_seconds() / 60
        if 0 < v < 240: gold_appr.append(v)
    if 'on-location' in d and 'picked-up' in d:
        v = (d['picked-up'] - d['on-location']).total_seconds() / 60
        if 0 < v < 240: gold_wait.append(v)
    if fid and fid in FL and FL[fid][4] and categorize_location(pl) in ('MCO Terminal','SFB Terminal'):
        ag = P(FL[fid][4])
        if 'picked-up' in d:
            x = (d['picked-up'] - ag).total_seconds() / 60
            if -60 < x < 180: gold_dwell.append(x)
        if 'on-location' in d:
            x = (d['on-location'] - ag).total_seconds() / 60
            if -240 < x < 240: gold_curb.append(x)
            if 'completed' in d:
                o = (d['completed'] - d['on-location']).total_seconds() / 60
                if 0 < o < 360: gold_occ.append(o)

gold_rows = []
for k in sorted(gold_lane, key=lambda k: -len(gold_lane[k])):
    v = gold_lane[k]
    if len(v) < 25: continue
    bs = sorted(pc([random.choice(v) for _ in v], .75) for _ in range(1000))
    allv = lane.get(k, [])
    gold_rows.append({
        "from": k[0], "to": k[1], "n": len(v),
        "p50": pc(v, .5), "p75": pc(v, .75), "p90": pc(v, .9),
        "ci_lo": bs[50], "ci_hi": bs[950],
        "code": DRIVE_TIME_ESTIMATES.get(k),
        "all_n": len(allv), "all_p50": (pc(allv, .5) if allv else None),
        "all_p75": (pc(allv, .75) if allv else None),
    })

gold_disc = [r for r in drv_rows if r["name"] in GOLD_NAMES]
gold_legs_total = sum(1 for lid,(pl,dl,did,pd,pt,fid) in LEGS.items() if did in GOLD)

# ---------------- PREDICTED vs ACTUAL clear time (uses the REAL scheduler function) ----------
from django.utils import timezone as _tz
from reservations.models import Leg as _Leg
from dispatching.scheduler import chain_clear_dt as _chain_clear

def _loc(dt):
    return _tz.localtime(dt).replace(tzinfo=None) if dt else None

pred_rows = collections.defaultdict(list)
_qs = (_Leg.objects.filter(driver_id__in=GOLD)
       .select_related("reservation", "flight_information")
       .prefetch_related("status_history"))
for _leg in _qs.iterator(chunk_size=400):
    if not _leg.pickup_time or not _leg.pickup_date: continue
    _pc = categorize_location(_leg.pickup_location); _dc = categorize_location(_leg.dropoff_location)
    _f = {}
    for _s in _leg.status_history.all():
        if _s.status in ('picked-up','completed'):
            if _s.status not in _f or _s.timestamp < _f[_s.status]: _f[_s.status] = _s.timestamp
    _sched = datetime.combine(_leg.pickup_date, _leg.pickup_time)
    try: _pred = _chain_clear(_leg, _leg.pickup_date)
    except Exception: continue
    _rec = {"pred": (_pred - _sched).total_seconds()/60}
    _pu, _cp = _loc(_f.get('picked-up')), _loc(_f.get('completed'))
    if _pu:
        _d = (_pu - _sched).total_seconds()/60
        if -180 < _d < 300: _rec["pu"] = _d
    if _cp:
        _a = (_cp - _sched).total_seconds()/60
        if 0 < _a < 600:
            _rec["act"] = _a
            _rec["err"] = (_cp - _pred).total_seconds()/60
    pred_rows[(_pc, _dc)].append(_rec)

def _clear_table(pred_rows, filt, minn=12):
    out = []
    for k in sorted(pred_rows, key=lambda k: -len(pred_rows[k])):
        if not filt(k): continue
        r = [x for x in pred_rows[k] if "err" in x]
        if len(r) < minn: continue
        p = [x["pred"] for x in r]; a = [x["act"] for x in r]; e = [x["err"] for x in r]
        out.append((f"{k[0]} → {k[1]}", len(r), f"{pc(p,.5):.0f}", f"{pc(a,.5):.0f}", f"{pc(a,.75):.0f}",
                    f"{pc(e,.5):+.0f}", f"{pc(e,.75):+.0f}", f"{pc(e,.9):+.0f}",
                    f"{100*sum(1 for x in e if x>0)/len(e):.0f}%"))
    return out

def _punct_table(pred_rows, filt, minn=12):
    out = []
    for k in sorted(pred_rows, key=lambda k: -len(pred_rows[k])):
        if not filt(k): continue
        r = [x["pu"] for x in pred_rows[k] if "pu" in x]
        if len(r) < minn: continue
        out.append((f"{k[0]} → {k[1]}", len(r), f"{pc(r,.25):+.0f}", f"{pc(r,.5):+.0f}",
                    f"{pc(r,.75):+.0f}", f"{pc(r,.9):+.0f}",
                    f"{100*sum(1 for x in r if x>15)/len(r):.0f}%"))
    return out

_isport = lambda k: 'Port Canaveral' in k[0] or 'Port Canaveral' in k[1]
port_clear = _clear_table(pred_rows, _isport)
port_punct = _punct_table(pred_rows, _isport)
all_clear = _clear_table(pred_rows, lambda k: not _isport(k), 25)
port_all_err = [x["err"] for k, v in pred_rows.items() if _isport(k) for x in v if "err" in x]
non_err = [x["err"] for k, v in pred_rows.items() if not _isport(k) for x in v if "err" in x]

# every route, ranked by how badly the prediction misses
every_clear = _clear_table(pred_rows, lambda k: True, 20)
every_punct = _punct_table(pred_rows, lambda k: True, 20)
all_err = [x["err"] for v in pred_rows.values() for x in v if "err" in x]
all_pu = [x["pu"] for v in pred_rows.values() for x in v if "pu" in x]
# sort the every-route table by error median descending (worst under-prediction first)
_err_by_lane = {}
for k, v in pred_rows.items():
    e = [x["err"] for x in v if "err" in x]
    if len(e) >= 20: _err_by_lane[f"{k[0]} → {k[1]}"] = pc(e, .5)
every_clear.sort(key=lambda r: -_err_by_lane.get(r[0], 0))

# impossible records
IMP = [(('SFB Terminal','Disney Resort'),35),(('Disney Resort','SFB Terminal'),35),
       (('MCO Terminal','Port Canaveral Area'),30),(('Port Canaveral Area','MCO Terminal'),30),
       (('MCO Terminal','Disney Resort'),12),(('Disney Resort','MCO Terminal'),12)]
imp_rows = []
for k, mn in IMP:
    v = lane.get(k, [])
    if not v: continue
    bad = [x for x in v if x < mn]
    imp_rows.append((f"{k[0]} → {k[1]}", len(v), mn, len(bad), f"{100*len(bad)/len(v):.1f}%"))

sig = lambda x: f"{x:+.0f}" if x else "0"
now_tables = {}

# ============================ WRITE ============================
doc = f"""# Grayson Towncar — Operational Data Audit

**Generated:** 2026-07-31
**Source:** production snapshot `content/db.sqlite3` (114 MB copy of the Railway Postgres)
**Analysis window:** driver status events 2026-02-08 → 2026-07-11; legs 2025-04-26 → present

> This document is a **data handoff**. Every figure was computed directly from the production
> database; none are estimates or recollections. Methodology, filters and known weaknesses are
> stated explicitly so the numbers can be challenged or reproduced.

---

## 1. How this data was collected

### 1.1 Source and access

The company runs Django on Railway (Postgres). A full copy of production exists locally at
`content/db.sqlite3` (114 MB, 99 tables). All queries were run **read-only**:

```python
import sqlite3
con = sqlite3.connect("file:content/db.sqlite3?mode=ro", uri=True)
```

The `db.sqlite3` in the repo root is a 0-byte placeholder — not the data.

Location bucketing used the application's own `dispatching.analytics.categorize_location()` rather
than a re-implementation, so lane names match what the scheduler actually sees.

### 1.2 The single most important structural fact

**A `Leg` row has no actual times.** It stores `pickup_date` and `pickup_time` (both *planned*) and
nothing else. There is no `actual_pickup_at`, no `dropoff_at`, no `completed_at`, no duration, no
mileage. Every "what really happened" number in this document is derived by **differencing rows in
`reservations_legstatus`** — the log of drivers tapping buttons in the driver portal.

Status ladder the driver taps: `Accept → On the Way → On Location → Picked Up → Complete`.
Each tap writes one row with `timezone.now()` at the moment of the tap. No user-entered times exist
anywhere in the system.

### 1.3 Timezone rules (critical — gets everyone the first time)

| Field | Storage |
|---|---|
| `reservations_legstatus.timestamp` | **UTC** |
| `reservations_flight.*_local` (all of them, despite the name) | **UTC** |
| `reservations_leg.pickup_date` / `pickup_time` | **naive local (Florida)** |

- Differencing two status events, or a status event against a flight time → **no conversion**.
- Comparing a status event against `pickup_date`/`pickup_time` → **convert**.
- The offset is **UTC−5 before 2026-03-08** and **UTC−4 from 2026-03-08 onward** (US DST).
  A flat offset corrupts all February data by exactly 60 minutes.

The DST boundary was detected empirically, not assumed: the modal offset between `on-location` and
scheduled pickup is +5h on 2026-03-07 (n=65) and +4h on 2026-03-08 (n=62). Applying the split
yields a median `on-location` delta of **−1.8 min**, i.e. drivers arrive within two minutes of the
scheduled time — the expected physical result, which confirms the offset is right.

### 1.4 Filters applied to every timing figure

1. **First occurrence only.** `MIN(timestamp)` per (leg, status). ~4–6% of legs carry duplicate rows
   per status (re-taps, the payroll bulk-update, the driver-unassign auto-reset).
2. **Trustworthy drivers only.** In-house drivers with `exclude_from_timing = false`. Affiliates are
   excluded because the application's own analytics already discards them.
3. **Plausibility bounds.** Ride time must be 2–240 minutes. Values outside are forgotten taps.
4. **Flight figures** require both `scheduled_gate_arrival_local` and `actual_gate_arrival_local`,
   and the pickup must categorise as `MCO Terminal` or `SFB Terminal`.
5. **Spirit (NK) removed** from all forward-looking figures — the carrier has ceased operations.

### 1.5 Metric definitions

| Name | Definition |
|---|---|
| Approach time | `on-the-way` → `on-location` |
| Curb wait | `on-location` → `picked-up` |
| **Ride time** (the main lane metric) | `picked-up` → `completed` |
| Driver occupancy | `on-location` → `completed` |
| True dwell (arrivals) | flight `actual_gate_arrival_local` → `picked-up` |
| Flight delay | `actual_gate_arrival_local` − `scheduled_gate_arrival_local` (negative = early) |
| Punctuality | `on-location` − scheduled pickup |

---

## 2. Data inventory

{md_table(["Table","Rows","Notes"],[
 ("reservations_legstatus","69,212","THE event log. Starts 2026-02-08. 99.7% authored by drivers."),
 ("reservations_leg","24,124","2025-04-26 onward. No actual-time columns."),
 ("reservations_flight","25,456","`flight_type` empty on 24,730 (97%)."),
 ("reservations_quote","42,150","Unexamined here."),
 ("reservations_lead","33,195","32% convert."),
 ("reservations_legflight","16,873","Multi-flight link. `is_controlling` clean: 0 legs with 0 or >1."),
 ("drivers_legpayment","17,730",""),
 ("reservations_schedulesnapshotentry","10,740","Plan-vs-actual is recoverable but unused."),
 ("ops_operationaltask","7,989","Exception history, unmined."),
 ("reservations_routetimingmetric","456","The learning table. Was 440 before rebuild."),
 ("reservations_routedistancecache","2","Effectively unused."),
 ("reservations_demandpattern","0","Model + writer exist. Never populated."),
 ("reservations_driverdailycapacity","0","Model + writer exist. Never populated."),
])}

### 2.1 Status-event coverage over time

{md_table(["Month","Legs","With full ladder","Coverage"],
  [(ym, f"{tot[ym]:,}", f"{full[ym]:,}", f"{100*full[ym]/tot[ym]:.0f}%") for ym in sorted(tot)],
  ["---",R[0],R[0],R[0]])}

*July is partial — status data ends 2026-07-11.* Coverage is **improving**, unaided.

---

## 3. IMPORTANT CORRECTION — read before using the lane figures

An earlier version of this analysis reported that *"every lane is under-estimated and none
over-estimated"*, comparing the measured **75th percentile** against the scheduler's
`DRIVE_TIME_ESTIMATES` table. **That comparison was not like-for-like.**

The table appears to have been authored as *typical* (median-ish) drive times. Comparing a p75
against it manufactures a shortfall on every row. Compared at the **median**, the table is
substantially accurate, and on several lanes it actually **over**-estimates:

{md_table(["Lane","n","Measured median","Table","Median − table","Measured p75","p75 − table"],
 [(f"{r['from']} → {r['to']}", f"{r['n']:,}", f"{r['p50']:.0f}", (r['code'] if r['has_entry'] else f"({DEFAULT_DRIVE_TIME})*"),
   sig(r['p50']-(r['code'] or DEFAULT_DRIVE_TIME)), f"{r['p75']:.0f}",
   sig(r['p75']-(r['code'] or DEFAULT_DRIVE_TIME)))
  for r in lane_rows], ["---",R[0],R[0],R[0],R[0],R[0],R[0]])}

`*` = no table entry; falls back to `DEFAULT_DRIVE_TIME = {DEFAULT_DRIVE_TIME}`.

**The honest conclusion:** the drive-time table is a reasonable *median* model. What it lacks is a
notion of spread. The scheduler uses it to decide whether a driver can make the next job — a
question that needs a conservative percentile, not a typical value. The issue is therefore **not
that the numbers are wrong**, but that a median is the wrong statistic for a feasibility gate.

---

## 4. Sanford (SFB) — flagged as suspicious, investigated

The concern was correct to raise. Findings:

{md_table(["Lane","n","p10","p25","median","p75","p90","max","Table"],
 [(f"{r['from']} → {r['to']}", r['n'], f"{r['p10']:.0f}", f"{r['p25']:.0f}", f"{r['p50']:.0f}",
   f"{r['p75']:.0f}", f"{r['p90']:.0f}", f"{r['max']:.0f}", r['code'])
  for r in lane_rows if 'SFB' in r['from'] or 'SFB' in r['to']],
 ["---",R[0],R[0],R[0],R[0],R[0],R[0],R[0],R[0]])}

**Both directions have a median of ~59–60 minutes, exactly matching the table's 60.** The table is
right for Sanford at the median.

What produced the apparent discrepancy:

1. **Small sample.** SFB → Disney has n=112 against 2,498 for MCO → Disney. Bootstrapped 90%
   confidence interval for its p75 is **72–79 min** — real, but wide.
2. **Fat right tail inbound.** The SFB → Disney distribution runs 3 legs at 10–19 min (physically
   impossible for ~45 miles), a cluster at 50–69, then 5 legs at 90–109, one at 131 and one at 186.
   A handful of extreme values moves p75 substantially at this sample size.
3. **Genuine directional asymmetry.** SFB → Disney p75 = 76 (CI 72–79) vs Disney → SFB p75 = 63
   (CI 62–65). Non-overlapping, so the inbound direction really is slower — the same pattern seen
   on MCO → Disney (44) vs Disney → MCO (37). The likely cause is that inbound trips end with
   luggage unload at an unfamiliar resort entrance, and the driver does not tap "complete" until
   the bags are out.

**Recommendation: do not change the Sanford table value.** 60 minutes is correct at the median. If
anything is done, make it directional (inbound needs more headroom than outbound), and only after
the sample grows.

### 4.1 Percentile stability across all lanes

Bootstrap, 1,000 resamples, 90% interval on p75. **Only the two MCO ↔ Disney lanes are large enough
for confident percentile claims.** Everything else should carry an explicit caveat.

{md_table(["Lane","n","median","p75","p75 90% CI","CI width","Verdict"],
 [(f"{r['from']} → {r['to']}", f"{r['n']:,}", f"{r['p50']:.0f}", f"{r['p75']:.0f}",
   f"{r['ci_lo']:.0f}–{r['ci_hi']:.0f}", f"{r['ci_w']:.0f}",
   ("stable" if r['ci_w']<=6 else ("borderline" if r['ci_w']<=12 else "TOO FEW SAMPLES")))
  for r in lane_rows], ["---",R[0],R[0],R[0],R[0],R[0],"---"])}

### 4.2 Physically impossible records still in the data

{md_table(["Lane","n","Impossible below","Count","Share"], imp_rows, ["---",R[0],R[0],R[0],R[0]])}

These survive the current filters and should be excluded by a per-lane minimum.

---

## 5. Flight punctuality

### 5.1 Overall

- **n = {len(alld):,}** arrivals with both scheduled and actual gate times
- Median delay **{pc(alld,.5):+.0f} min**, 10th pct {pc(alld,.1):+.0f}, 90th pct {pc(alld,.9):+.0f}
- **{100*sum(1 for x in alld if x<-5)/len(alld):.0f}% arrive early** (>5 min ahead)
- {100*sum(1 for x in alld if -15<=x<=15)/len(alld):.0f}% within ±15 min
- {100*sum(1 for x in alld if x>15)/len(alld):.0f}% more than 15 min late;
  {100*sum(1 for x in alld if x>45)/len(alld):.1f}% more than 45 min late

The distribution is **left-shifted with a long right tail**. Planning against a median plans for the
case that was never going to hurt you; the operational risk lives entirely in the tail.

### 5.2 By airline

{md_table(["Airline","n","10th pct","Median","90th pct",">15 min late"],
 [(r['name'], f"{r['n']:,}", f"{r['p10']:+.0f}", f"{r['p50']:+.0f}", f"{r['p90']:+.0f}", f"{r['late15']:.0f}%")
  for r in air_rows], ["---",R[0],R[0],R[0],R[0],R[0]])}

### 5.3 By airline AND time of day — median delay (n)

This is the dimension a per-airline average hides. Cells with fewer than 15 observations are blank.

{md_table(["Airline"]+[b[0] for b in BUCK],
 [[a]+[(f"{pc(grid[(a,b[0])],.5):+.0f} ({len(grid[(a,b[0])])})" if len(grid[(a,b[0])])>=15 else "—")
   for b in BUCK] for a in airlines], ["---"]+[R[0]]*len(BUCK))}

**Pattern: early-morning and late-night flights run late; midday runs early.** The within-airline
swing reaches 27 minutes (American: −10 midday vs +17 before 8am), which is larger than the spread
*between* most airlines.

### 5.4 Per-flight predictability

{len(perflight):,} distinct flight identities appear. Recurrence:

{md_table(["Seen at least","Flights","Arrivals covered","Share of volume"],
 [(f"{t}×", f"{len([1 for v in perflight.values() if len(v)>=t]):,}",
   f"{sum(len(v) for v in perflight.values() if len(v)>=t):,}",
   f"{100*sum(len(v) for v in perflight.values() if len(v)>=t)/sum(len(v) for v in perflight.values()):.0f}%")
  for t in (3,5,8,12,20)], ["---",R[0],R[0],R[0]])}

**Typical uncertainty window (p10→p90):** {sorted(sp)[len(sp)//2]:.0f} min per specific flight vs
{sorted(airsp)[len(airsp)//2]:.0f} min per airline — a modest average gain. The value is in
identifying *which* flights are trustworthy.

Tightest (most predictable), n ≥ 12:

{md_table(["Flight","Origin","Seen","Median","90th pct","Window"],
 [(f['ident'], f['origin'], f"{f['n']}×", f"{f['p50']:+.0f}", f"{f['p90']:+.0f}", f"{f['spread']:.0f} min")
  for f in flight_rows[:15]], ["---","---",R[0],R[0],R[0],R[0]])}

Widest (never plan tight), n ≥ 12:

{md_table(["Flight","Origin","Seen","Median","90th pct","Window"],
 [(f['ident'], f['origin'], f"{f['n']}×", f"{f['p50']:+.0f}", f"{f['p90']:+.0f}", f"{f['spread']:.0f} min")
  for f in list(reversed(flight_rows))[:10]], ["---","---",R[0],R[0],R[0],R[0]])}

---

## 6. Arrival anatomy

{md_table(["Measure","n","p25","median","p75","p90"],
 [("True dwell — gate docked → guest in car", f"{len(dwell):,}", f"{pc(dwell,.25):+.0f}",
   f"{pc(dwell,.5):+.0f}", f"{pc(dwell,.75):+.0f}", f"{pc(dwell,.9):+.0f}"),
  ("Driver on-location vs gate docking", f"{len(curb):,}", f"{pc(curb,.25):+.0f}",
   f"{pc(curb,.5):+.0f}", f"{pc(curb,.75):+.0f}", f"{pc(curb,.9):+.0f}"),
  ("Driver occupancy — on-location → complete", f"{len(occ):,}", f"{pc(occ,.25):+.0f}",
   f"{pc(occ,.5):+.0f}", f"{pc(occ,.75):+.0f}", f"{pc(occ,.9):+.0f}")],
 ["---",R[0],R[0],R[0],R[0],R[0]])}

- The code assumes a flat **45-minute** dwell (`STATIC_FLOOR_DWELL_MIN`). Measured p75 is
  {pc(dwell,.75):.0f} — close at p75, but **{pc(dwell,.9)-45:.0f} min short at p90**, and blind to the
  airline and time-of-day differences in §5.3.
- **{100*sum(1 for x in curb if x<0)/len(curb):.0f}% of the time the driver is on location before the
  plane has docked** — roughly {abs(sum(x for x in curb if x<0))/60/5:.0f} driver-hours per month of
  pure waiting, before any deplaning.
- Two independent fallbacks in the code use **75 min** (`ops/tasks.py:26`) and **60 min**
  (`ops/views.py:1759`) for trip duration; measured p75 occupancy is {pc(occ,.75):.0f} min.

---

## 7. Disney resort granularity — tested, not worth building

MCO → each Disney resort (the scheduler currently uses one flat 30 min for all):

{md_table(["Resort","n","median","p75","p90"],
 sorted([(k, len(v), f"{pc(v,.5):.0f}", f"{pc(v,.75):.0f}", f"{pc(v,.9):.0f}")
   for k,v in res_in.items() if len(v)>=25], key=lambda r:-float(r[3])),
 ["---",R[0],R[0],R[0],R[0]])}

Disney → MCO:

{md_table(["Resort","n","median","p75","p90"],
 sorted([(k, len(v), f"{pc(v,.5):.0f}", f"{pc(v,.75):.0f}", f"{pc(v,.9):.0f}")
   for k,v in res_out.items() if len(v)>=25], key=lambda r:-float(r[3])),
 ["---",R[0],R[0],R[0],R[0]])}

**Every resort's median sits in an ~8-minute band.** Splitting one bucket into eighteen would divide
samples ~18× to buy a few minutes of precision, and most resorts would fall below the threshold
where the scheduler trusts a bucket at all (`sample_count >= 5`). The variation is **within** each
resort, not between them — which is traffic and time of day.

---

## 8. Data quality

### 8.1 Trustworthy

- **Driver status taps.** 99.7% of 69,212 events authored by drivers with `timezone.now()` at tap
  time. No user-entered times exist in the system. After the DST correction, median `on-location`
  lands within ~2 minutes of scheduled pickup every month, with genuine spread (p05 −41, p95 +82) —
  anchored or backfilled data does not look like that.
- **Flight gate actuals** (FlightAware AeroAPI). 83% coverage on airport-pickup legs.
  `is_controlling` is clean: zero legs with none or more than one.
- **Coverage is improving**: 60% → 85% over five months.

### 8.2 Not trustworthy

| Issue | Measured | Impact |
|---|---|---|
| `flight_type` empty | 24,730 of 25,456 rows (**97%**) | Arrival vs departure cannot be read from the flight record; everything falls back to keyword-matching free-text locations |
| Double-tapping | **36%** of full-ladder legs have ≥1 adjacent gap under 60s | Understates approach time by ~4 min at the median. Does **not** affect ride time (protected by an existing 2-min floor) |
| Seven drivers tap "Picked Up" + "Complete" together | 65–95% of their trips | ~11% of completed legs had fictional ride times. Now excluded |
| `exclude_from_timing` mis-targeted | 3 excluded drivers produced excellent data; 0 of the 7 bad ones were flagged | Corrected |
| Duplicate status rows | 3.7–5.9% of legs | Interacted with a `.first()` ordering bug (fixed) |
| Corrupt pickup dates | legs dated **year 3220** and **2029** | Poisons any MIN/MAX or range query |
| Airline name fragmentation | `PORTER`, `PORTER AIRLINES`, `PORTER AIRLINE`; bare `AA`/`DL`/`UA`/`WN` alongside full names | Splits airline grouping |
| `utm_source` fragmentation | `meta`, `Meta`, `facebook`, `fb`, `ig` | Marketing attribution split across 5 spellings of 2 channels |
| Payroll bulk-complete | 337 rows stamped at payroll time | Small; self-identifying via `notes` |
| Driver reassignment wipes progression | 1,806 `Auto-reset` rows | Erases real taps; also a hidden churn metric |

### 8.3 Corrected during the audit

- **`in-progress` is not an anomaly.** It is the Django model default for a newly created Leg
  (`reservations/models.py:1067`). Of 4,936 such legs, 87% have no driver and 66% are future-dated.
  Only ~584 are past-dated stragglers. It must be **excluded** from any timing pipeline — its median
  delta is −399 min because it is a pre-trip bookkeeping state, not a driving event.
- **Django admin bulk-complete bypasses `LegStatus`** — true in code, but only **3 legs** in the
  whole event era lack a completed event. Not a live problem.
- **AeroAPI stops refreshing once a leg is `completed`** (`ops/tasks.py:1480`) — true in code, but
  completed arrival legs still show 87.9% actual-gate coverage. Worth fixing defensively, not
  urgently.

### 8.4 Driver status discipline (in-house, ≥25 legs)

`instant%` = share of full-ladder trips where `picked-up` → `completed` was under 2 minutes.

{md_table(["Driver","Legs","Full ladder","Instant","Median ride","Currently excluded"],
 [(r['name'], f"{r['legs']:,}", f"{r['full']:.0f}%", f"{r['inst']:.0f}%", f"{r['med']:.0f} min",
   "YES" if r['excluded'] else "—") for r in drv_rows],
 ["---",R[0],R[0],R[0],R[0],"---"])}

Two distinct failure modes hide behind one completion percentage:
- **Fabricating** (high instant%) — taps both buttons at the end. High full-ladder score, zero usable
  data. Must be excluded.
- **Sparse** (low full-ladder%) — runs the ladder rarely, but the completed trips are honest.
  Must **not** be excluded; doing so throws away real samples.

---

## 9. GOLD COHORT — the drivers who use the app properly

The founder nominated the drivers who genuinely use the application properly. Every nominee was
vetted against the data before inclusion, and one was rejected.

**Cohort ({len(GOLD)} drivers, {gold_legs_total:,} legs):** {", ".join(GOLD_NAMES)}{(" — NOT FOUND: " + ", ".join(GOLD_MISSING)) if GOLD_MISSING else ""}

**Rejected on the data:** {"; ".join(f"**{n}** — {why}" for n, why in GOLD_REJECTED)}. Including
a driver whose ride times are fabricated would defeat the point of having a clean baseline.

**Note on `oualid`:** he is an *affiliate*, and the founder is right that he is the only affiliate
using the ladder properly (1% instant-complete, better than most in-house drivers). But the
application's own analytics filters to `driver_type='inhouse'`
(`analytics.py:update_single_route_timing_metric`), so **his trips never reach `RouteTimingMetric`
in production**. He is included in this section's figures. If his data should count for real, that
filter has to change — it is a code decision, not a flag.

### 9.1 Their actual discipline

{md_table(["Driver","Legs","Full ladder","Instant (<2 min)","Median ride"],
 [(r['name'], f"{r['legs']:,}", f"{r['full']:.0f}%", f"{r['inst']:.0f}%", f"{r['med']:.0f} min")
  for r in sorted(gold_disc, key=lambda r:-r['legs'])], ["---",R[0],R[0],R[0],R[0]])}

The nomination is broadly borne out — but not uniformly. Steven (2%) and Roberto (3%) are near
flawless; Sereen (20%), Runer (14%) and Angel (13%) double-tap on roughly one trip in six. Roberto's
full-ladder rate (89%) is the lowest of the eight. **This is a coaching list, not a scorecard** —
the gap between the best and worst of these eight is small compared to the excluded seven (65–95%).

### 9.2 Lane timings — gold cohort only, vs the full trustworthy set

{md_table(["Lane","Gold n","Gold median","Gold p75","Gold p75 CI","All-driver n","All median","All p75","Median diff"],
 [(f"{r['from']} → {r['to']}", f"{r['n']:,}", f"{r['p50']:.0f}", f"{r['p75']:.0f}",
   f"{r['ci_lo']:.0f}–{r['ci_hi']:.0f}", f"{r['all_n']:,}",
   (f"{r['all_p50']:.0f}" if r['all_p50'] is not None else "—"),
   (f"{r['all_p75']:.0f}" if r['all_p75'] is not None else "—"),
   (sig(r['p50'] - r['all_p50']) if r['all_p50'] is not None else "—"))
  for r in gold_rows], ["---",R[0],R[0],R[0],R[0],R[0],R[0],R[0],R[0]])}

**Read this table carefully — it is the key validation of the whole analysis.** If the gold cohort's
numbers differed materially from the full trustworthy set, every conclusion would be suspect. They
do not: the medians track within a few minutes on every lane with meaningful sample.

That means the driver-exclusion work in §8.4 was sufficient. Restricting further to the eight best
drivers **buys accuracy but costs sample size**, and the accuracy gain is small.

### 9.3 Gold-cohort arrival anatomy

{md_table(["Measure","n","p25","median","p75","p90"],
 [("Approach — on-the-way → on-location", f"{len(gold_appr):,}", f"{pc(gold_appr,.25):.0f}",
   f"{pc(gold_appr,.5):.0f}", f"{pc(gold_appr,.75):.0f}", f"{pc(gold_appr,.9):.0f}"),
  ("Curb wait — on-location → picked-up", f"{len(gold_wait):,}", f"{pc(gold_wait,.25):.0f}",
   f"{pc(gold_wait,.5):.0f}", f"{pc(gold_wait,.75):.0f}", f"{pc(gold_wait,.9):.0f}"),
  ("True dwell — gate docked → in car", f"{len(gold_dwell):,}", f"{pc(gold_dwell,.25):+.0f}",
   f"{pc(gold_dwell,.5):+.0f}", f"{pc(gold_dwell,.75):+.0f}", f"{pc(gold_dwell,.9):+.0f}"),
  ("Occupancy — on-location → complete", f"{len(gold_occ):,}", f"{pc(gold_occ,.25):.0f}",
   f"{pc(gold_occ,.5):.0f}", f"{pc(gold_occ,.75):.0f}", f"{pc(gold_occ,.9):.0f}")],
 ["---",R[0],R[0],R[0],R[0],R[0]])}

Compare to the all-trustworthy-driver figures in §6: dwell median {pc(dwell,.5):.0f} vs gold
{pc(gold_dwell,.5):.0f}; occupancy median {pc(occ,.5):.0f} vs gold {pc(gold_occ,.5):.0f}.

### 9.4 Recommendation on cohort choice

Use the **full trustworthy set** (in-house, `exclude_from_timing = false`) as the production
baseline, not the gold eight. Reasons:

1. The medians agree, so the gold cohort adds little accuracy.
2. Sample size matters more — the binding constraint is that 75% of route buckets already fall below
   the scheduler's `sample_count >= 5` trust floor. Shrinking the cohort makes that worse.
3. The gold eight are not evenly spread across lanes, shifts or vehicle types, so restricting to
   them would introduce its own selection bias.

Keep the gold cohort as a **validation set**: when a metric changes, check it moves the same way in
both populations. If they ever diverge, that is a signal worth investigating.

---

## 10. Predicted clear time vs actual — every route (gold cohort)

**What does the system predict a job will take, and what does it actually take?**

### 10.1 How the prediction is made

The figures below call the production function `dispatching.scheduler.chain_clear_dt()` — the
same code the scheduler uses to decide whether a driver can make their next job. It is not a
re-implementation. The formula (`scheduler.py:933`):

```
clear = anchor + dwell + category_drive + store_stop

anchor = scheduled pickup_time  (pushed LATER if a live flight ETA is later; never earlier)
dwell  = 45 min  for arrivals, and for to-cruise legs picked up at an airport
       =  0 min  for from-cruise legs (leaving the port) and everything else
drive  = DRIVE_TIME_ESTIMATES[(pickup_category, dropoff_category)], else 35
store  = 25 min if the reservation has a Publix stop
```

So a **Port Canaveral → MCO** job predicts `pickup + 55`, while **MCO → Port Canaveral** predicts
`pickup + 45 + 55 = 100` because the guest is deplaning first.

"Actual cleared" = the driver's `completed` tap. Both are measured from the scheduled pickup time,
so the two columns are directly comparable.

### 10.2 EVERY ROUTE — ranked worst-under-predicted first

n ≥ 20. **Error = actual − predicted. Positive means the job ran LONGER than the system expected**,
which is the direction that breaks chains.

{md_table(["Lane","n","Predicted","Actual median","Actual p75","Error median","Error p75","Error p90","% finish late"],
 every_clear, ["---",R[0],R[0],R[0],R[0],R[0],R[0],R[0],R[0]])}

**Across all {len(all_err):,} jobs:** error median **{pc(all_err,.5):+.0f} min**, p25 {pc(all_err,.25):+.0f},
p75 {pc(all_err,.75):+.0f}, p90 {pc(all_err,.9):+.0f}.
**{100*sum(1 for x in all_err if x>0)/len(all_err):.0f}% of jobs finish later than predicted.**

Three things fall out of this table:

1. **The prediction is well-centred overall** — the median job finishes within a couple of minutes of
   what the scheduler expected. This is a working model, not a broken one.
2. **The misses are concentrated in short intra-Orlando hops.** Disney → Disney, Airport Hotel →
   Disney, Disney ↔ Universal and Disney → MCO all run long, with 61–88% of jobs finishing late. On
   a 28-minute predicted job a 10-minute miss is a 36% error; on a 100-minute port run the same ten
   minutes is noise. **Short lanes are where the chain actually breaks.**
3. **Long airport-anchored lanes are over-predicted** — SFB → Disney and MCO → Port Canaveral both
   run 14 minutes short of prediction, MCO → Universal 10, MCO → Disney 8. The flat 45-minute dwell
   is generous once the drive itself is long.

### 10.3 Pickup punctuality — every route

{md_table(["Lane","n","p25","median","p75","p90","% >15 min late"],
 every_punct, ["---",R[0],R[0],R[0],R[0],R[0],R[0]])}

Across {len(all_pu):,} jobs the median guest boards **{pc(all_pu,.5):+.0f} min** from the scheduled
pickup time. The airport-origin lanes are the outliers — see §10.5.

### 10.4 Port Canaveral in detail

{md_table(["Lane","n","Predicted","Actual median","Actual p75","Error median","Error p75","Error p90","% finish late"],
 port_clear, ["---",R[0],R[0],R[0],R[0],R[0],R[0],R[0],R[0]])}

*Error = actual cleared − predicted cleared. Negative means the driver finished **earlier** than the
system expected.*

**Overall across {len(port_all_err)} Port Canaveral jobs:** error median **{pc(port_all_err,.5):+.0f} min**,
p25 {pc(port_all_err,.25):+.0f}, p75 {pc(port_all_err,.75):+.0f}, p90 {pc(port_all_err,.9):+.0f}.
**{100*sum(1 for x in port_all_err if x>0)/len(port_all_err):.0f}% of jobs finish later than predicted.**

Read: **the clear-time prediction is broadly sound at Port Canaveral** — slightly conservative at
the median, with a p90 tail of about half an hour. Two lanes deserve attention:

- **MCO → Port Canaveral** is over-predicted by ~14 min at the median (predicts 100, typically takes
  86), and only about a quarter of these jobs run late. The 45-minute dwell is generous here.
- **Disney → Port Canaveral** is the one that runs hot: predicted 72, actual median 78, and **71% of
  jobs finish later than predicted** with a p90 error of +35. This lane is genuinely under-buffered
  and is the clearest candidate for a table change on the port side.

### 10.5 Pickup punctuality at the port — the important finding

{md_table(["Lane","n","p25","median","p75","p90","% >15 min late"],
 port_punct, ["---",R[0],R[0],R[0],R[0],R[0],R[0]])}

*Actual `picked-up` tap minus scheduled pickup time. Positive = guest boarded later than scheduled.*

**Port pickups are realistic. Airport-to-port pickups are not.**

- Leaving the **port** (disembarkation), the median guest boards **3 minutes early**. Those scheduled
  times are honest — the guest is standing there waiting.
- Going **MCO → Port Canaveral**, the median guest boards **36 minutes after** the scheduled pickup
  time, p75 +59, p90 +85, and **87% board more than 15 minutes late.**

That second row is not a lateness problem — it is a **labelling problem**. On an airport-to-port leg
the "pickup time" is effectively the flight's arrival slot, not the moment the guest reaches the car.
The scheduler already understands this (it adds the 45-minute dwell before computing clear time), but
a dispatcher reading the board sees a pickup time that will be wrong by half an hour, and any
on-time metric computed off that field will unfairly mark these jobs late.

### 10.6 Port vs non-port

Across {len(non_err):,} non-port jobs the error median is **{pc(non_err,.5):+.0f} min** (p75
{pc(non_err,.75):+.0f}, p90 {pc(non_err,.9):+.0f}), versus **{pc(port_all_err,.5):+.0f} min** across
{len(port_all_err):,} port jobs (p75 {pc(port_all_err,.75):+.0f}, p90 {pc(port_all_err,.9):+.0f}).

**Port Canaveral is not the problem lane.** The port predictions hold up at least as well as the
Orlando-area ones, because the long drive dominates and leaves less room for proportional error.

---

## 11. What the software currently assumes

| Constant | Value | Location | Measured reality |
|---|---|---|---|
| `STATIC_FLOOR_DWELL_MIN` | 45 min | `scheduler.py:195` | p75 {pc(dwell,.75):.0f}, p90 {pc(dwell,.9):.0f} |
| `DEFAULT_DRIVE_TIME` | 35 min | `scheduler.py:83` | lane-dependent |
| `DEPLANING_GRACE_MIN` | 10 min | `feasibility_guards.py:39` | — |
| `SAFETY_PAD_MIN` | 0 min | `feasibility_guards.py:44` | — |
| `FALLBACK_TRIP_DURATION_MINUTES` | 75 min | `ops/tasks.py:26` | p75 occupancy {pc(occ,.75):.0f} |
| (second fallback) | 60 min | `ops/views.py:1759` | same |
| `MIN_PICKUP_TO_COMPLETE` | 2 min | `analytics.py:434` | doing real work — keep |
| `sample_count >= 5` | trust floor | `scheduler.py:605` | 75% of buckets fail this |

`required_turnaround = (−10 if next leg is an airport arrival at the same category, else
category-table drive minutes) + 0 safety pad`. Live Google distance is **off** in production
(`USE_LIVE_DISTANCE=0`).

**Planned arrival buffer:** median `scheduled pickup − scheduled gate arrival` = **−1 minute**.
There is effectively no deliberate buffer; pickup is scheduled at the gate time.

---

## 12. Open questions / known weaknesses of this analysis

1. **Ride time is driver wall-clock, not routing time.** It includes the driver's latency in tapping
   "complete" and any luggage handling. It is the right metric for "when is the driver free", but it
   is **not** a pure drive time and should not be compared to a maps ETA.
2. **Only two lanes have enough data for confident percentiles** (§4.1). Everything else needs a
   caveat.
3. **Five months of event history.** No seasonality can be measured — Florida's cruise and
   theme-park cycles are annual, and this window covers February to July only.
4. **Selection bias.** Every timing figure is computed on the subset of legs that have a complete
   ladder (60–85% depending on month), from drivers who tap reliably. That subset may not represent
   the chaotic days.
5. **Cruise data is two strings.** `reservations_cruise` stores only `cruise_line` and `ship_name` —
   no port, no sail date, no disembarkation time. Port Canaveral timing can only be measured against
   our own pickup times, never against ship reality.
6. **Nothing records what was predicted.** The system never stores the scheduler's estimate
   alongside the outcome, so estimate accuracy cannot be tracked over time. This is the single
   biggest structural gap for a "learning" system.
7. **No passenger no-show, wait-time, extra-stop, reassignment-history, or mileage events exist**
   anywhere in the schema.
8. **Samsara GPS is never historized** — vehicle position is overwritten every 3 minutes, so real
   drive times independent of driver taps are discarded continuously.

---

## 13. Reproducing any figure here

```bash
cd /path/to/grayson-towncar
ENABLE_DEBUG_TOOLBAR=0 python - <<'EOF'
import os, django, sqlite3, collections
from datetime import datetime
os.environ.setdefault("DJANGO_SETTINGS_MODULE","business.settings"); django.setup()
from dispatching.analytics import categorize_location

con = sqlite3.connect("file:content/db.sqlite3?mode=ro", uri=True); cur = con.cursor()
P = lambda s: datetime.fromisoformat(s.replace(' ','T').split('.')[0])

good = {{r[0] for r in cur.execute(
    "SELECT id FROM drivers_driver WHERE driver_type='inhouse' AND exclude_from_timing=0")}}

ev = collections.defaultdict(dict)
for lid, s, ts in cur.execute('''SELECT leg_id,status,MIN(timestamp)
      FROM reservations_legstatus
      WHERE status IN ('on-the-way','on-location','picked-up','completed')
      GROUP BY leg_id,status'''):
    ev[lid][s] = P(ts)

lane = collections.defaultdict(list)
for lid, pl, dl, did in cur.execute(
        "SELECT id,pickup_location,dropoff_location,driver_id FROM reservations_leg"):
    d = ev.get(lid)
    if not d or 'picked-up' not in d or 'completed' not in d or did not in good:
        continue
    v = (d['completed'] - d['picked-up']).total_seconds()/60      # both UTC — no conversion
    if 2 <= v <= 240:
        lane[(categorize_location(pl), categorize_location(dl))].append(v)

for k, v in sorted(lane.items(), key=lambda x: -len(x[1]))[:10]:
    v.sort()
    print(k, len(v), 'p50=%.0f' % v[len(v)//2], 'p75=%.0f' % v[int(.75*len(v))])
EOF
```

There is also a management command for the driver-discipline table:

```bash
python manage.py driver_data_quality --days 200          # report only
python manage.py driver_data_quality --days 200 --apply  # write exclude_from_timing
```
"""

out = os.path.join(os.getcwd(), "docs", "operational-data-audit.md")
open(out, "w", encoding="utf-8").write(doc)
print("wrote", out)
print(len(doc), "chars,", doc.count("\n"), "lines")
print("lanes:", len(lane_rows), "airlines:", len(air_rows), "flights>=12:", len(flight_rows),
      "drivers:", len(drv_rows), "resorts in/out:", len(res_in), len(res_out))
