# Deterministic review of the 2026-06-14 auto-assign-all schedule.
# Mirrors dispatching/scheduler.py: DRIVE_TIME_ESTIMATES categories, 45-min dwell,
# 15-min deplaning grace, CLEAR_BY window mode.
import csv, sys
from datetime import datetime, timedelta
from collections import defaultdict

CSV = sys.argv[1] if len(sys.argv) > 1 else r"c:\Users\admin\OneDrive\Desktop\grayson-towncar\.analysis\legs_sunday_after_autoassign.csv"

DRIVE = {
    ('MCO Terminal','Disney Resort'):30, ('Disney Resort','MCO Terminal'):30,
    ('MCO Terminal','Universal Resort'):25, ('Universal Resort','MCO Terminal'):25,
    ('MCO Terminal','Port Canaveral Area'):55, ('Port Canaveral Area','MCO Terminal'):55,
    ('MCO Terminal','Other Hotel'):25, ('Other Hotel','MCO Terminal'):25,
    ('MCO Terminal','Residential'):30, ('Residential','MCO Terminal'):30,
    ('MCO Terminal','Airport Hotel'):12, ('Airport Hotel','MCO Terminal'):12,
    ('Disney Resort','Port Canaveral Area'):72, ('Port Canaveral Area','Disney Resort'):72,
    ('Disney Resort','Universal Resort'):28, ('Universal Resort','Disney Resort'):28,
    ('Disney Resort','Other Hotel'):25, ('Other Hotel','Disney Resort'):25,
    ('Disney Resort','Disney Resort'):12, ('MCO Terminal','MCO Terminal'):2,
    ('SFB Terminal','SFB Terminal'):2, ('Airport Hotel','Airport Hotel'):10,
    ('Other Hotel','Other Hotel'):15, ('Residential','Residential'):15,
    ('Port Canaveral Area','Port Canaveral Area'):10, ('Other','Other'):20,
    ('Universal Resort','Port Canaveral Area'):60, ('Port Canaveral Area','Universal Resort'):60,
    ('Universal Resort','Other Hotel'):15, ('Other Hotel','Universal Resort'):15,
    ('Universal Resort','Universal Resort'):10,
    ('SFB Terminal','Disney Resort'):60, ('Disney Resort','SFB Terminal'):60,
    ('SFB Terminal','Universal Resort'):49,
    ('SFB Terminal','Port Canaveral Area'):70, ('Port Canaveral Area','SFB Terminal'):70,
    ('Airport Hotel','Disney Resort'):25, ('Disney Resort','Airport Hotel'):25,
    ('Airport Hotel','Universal Resort'):20, ('Universal Resort','Airport Hotel'):20,
    ('SFB Terminal','MCO Terminal'):60, ('MCO Terminal','SFB Terminal'):60,
    ('SFB Terminal','Other Hotel'):55, ('Other Hotel','SFB Terminal'):55,
    ('SFB Terminal','Airport Hotel'):45, ('Airport Hotel','SFB Terminal'):45,
    ('SFB Terminal','Residential'):55, ('Residential','SFB Terminal'):55,
}
DEFAULT_DRIVE = 35
DWELL = 45
GRACE = 15
# routes the table is missing/optimistic about (used for "realistic" slack only)
REAL_OVERRIDE = {
    ('Airport Hotel','Port Canaveral Area'):55, ('Port Canaveral Area','Airport Hotel'):55,
    ('Other Hotel','Port Canaveral Area'):55, ('Port Canaveral Area','Other Hotel'):55,
    ('Universal Resort','SFB Terminal'):49,
    ('Port Canaveral Area','Residential'):75, ('Residential','Port Canaveral Area'):75,
}

def cat(loc):
    s = loc.lower()
    if any(k in s for k in ('port canaveral','cocoa','cape canaveral','cruise terminal')): return 'Port Canaveral Area'
    if any(k in s for k in ('embassy suites','hyatt place','holiday inn','marriott orlando airport lakeside')): return 'Airport Hotel'
    if 'mco' in s or 'orlando international' in s: return 'MCO Terminal'
    if 'sanford' in s or 'sfb' in s: return 'SFB Terminal'
    if any(k in s for k in ('universal','helios','cabana bay','portofino','hard rock','stella nova','hyatt house')): return 'Universal Resort'
    if any(k in s for k in ('buena vista palace','wyndham garden','fairfield','renaissance')): return 'Disney Resort'
    if any(k in s for k in ('disney','lake buena vista','bay lake','polynesian','grand floridian','contemporary','boardwalk','epcot','animal kingdom','saratoga','old key west','pop century','art of animation','caribbean beach','coronado','yacht club','beach club','riviera','wilderness lodge','copper creek','all-star','port orleans','dolphin','swan','shades of green','kidani','jambo','island tower','mandara','kissimmee')): return 'Disney Resort'
    if 'hilton orlando' in s: return 'Other Hotel'
    if 'clermont' in s or 'winter garden' in s: return 'Residential'
    return 'Other Hotel'

def drive(a,b,real=False):
    if real and (a,b) in REAL_OVERRIDE: return REAL_OVERRIDE[(a,b)]
    return DRIVE.get((a,b), DEFAULT_DRIVE)

TIER = {'towncar':1,'mini van':2,'suv':3,'van':4,'van (14 pax)':5}
DRIVER_TYPE = {  # native type per the schedule board
    'Yovanny Suarez':'suv','rizwan':'mini van','sereen':'suv','roberto':'van (14 pax)',
    'george':'van','runer':'suv','HassanA':'suv','David Encarancion':'van (14 pax)',
    'Idrees':'suv','Aftab':'mini van','ken':'van (14 pax)','Raymond':'van (14 pax)',
    'Steven Kleisath':'towncar'}
WINDOW = {'Yovanny Suarez':(6,18),'runer':(3,19),'Steven Kleisath':(6,23)}  # from board; others Flexible
PREV_CLEAR = {  # previous-night cleared, from the board
    'george':'22:31','rizwan':'22:31','roberto':'22:40','runer':'18:32','sereen':'17:00',
    'ken':'21:14','Raymond':'21:55','Idrees':'18:25'}

D0 = datetime(2026,6,14)
def pdt(s):
    return datetime.strptime('2026-06-14 '+s.strip(), '%Y-%m-%d %I:%M %p')

legs=[]
with open(CSV, encoding='utf-8', errors='replace') as f:
    for r in csv.DictReader(f):
        L = dict(r)
        L['dt'] = pdt(r['pickup_time'])
        L['pc'] = cat(r['pickup_location']); L['dc'] = cat(r['dropoff_location'])
        L['vt'] = r['vehicle_type'].strip().lower()
        L['tier'] = TIER[L['vt']]
        L['pax'] = int(r['passenger_count'])
        L['is_arr'] = r['trip_type']=='Arrival' or r['trip_type']=='Cruise (MCO)'  # both anchored to a flight at MCO
        L['is_cruise'] = 'Cruise' in r['trip_type']
        legs.append(L)

def clear_dt(L, real=False):
    d = drive(L['pc'], L['dc'], real)
    if L['is_arr']: return L['dt'] + timedelta(minutes=DWELL+d)
    return L['dt'] + timedelta(minutes=d)

def chain_buffers(prev, nxt):
    """(scheduler_buffer_min, realistic_slack_min) for prev -> nxt"""
    pc_s, pc_r = clear_dt(prev), clear_dt(prev, True)
    if nxt['is_arr']:
        repo = 0 if prev['dc']=='MCO Terminal' else drive(prev['dc'],'MCO Terminal')
        sched = (nxt['dt'] - (pc_s + timedelta(minutes=repo-GRACE))).total_seconds()/60
        repo_r = 0 if prev['dc']=='MCO Terminal' else drive(prev['dc'],'MCO Terminal',True)
        real = (nxt['dt'] + timedelta(minutes=DWELL) - (pc_r + timedelta(minutes=repo_r))).total_seconds()/60
    else:
        repo = drive(prev['dc'], nxt['pc']); repo_r = drive(prev['dc'], nxt['pc'], True)
        sched = (nxt['dt'] - (pc_s + timedelta(minutes=repo))).total_seconds()/60
        real = (nxt['dt'] - (pc_r + timedelta(minutes=repo_r))).total_seconds()/60
    return sched, real

assigned = [l for l in legs if l['assigned_driver']!='Unassigned']
unassigned = sorted([l for l in legs if l['assigned_driver']=='Unassigned'], key=lambda l:l['dt'])
byd = defaultdict(list)
for l in assigned: byd[l['assigned_driver']].append(l)
for d in byd: byd[d].sort(key=lambda l:l['dt'])

W = sys.stdout.write
W("="*78+"\nTOTALS\n"+"="*78+"\n")
W(f"legs={len(legs)} assigned={len(assigned)} unassigned={len(unassigned)} ({100*len(unassigned)/len(legs):.0f}%)\n")
W(f"pax total={sum(l['pax'] for l in legs)} assigned={sum(l['pax'] for l in assigned)} unassigned={sum(l['pax'] for l in unassigned)} ({100*sum(l['pax'] for l in unassigned)/sum(l['pax'] for l in legs):.0f}%)\n")

W("\nCOVERAGE BY VEHICLE TYPE (jobs, pax)\n")
for vt in ['van (14 pax)','van','suv','mini van','towncar']:
    t=[l for l in legs if l['vt']==vt]; u=[l for l in t if l['assigned_driver']=='Unassigned']
    W(f"  {vt:14s} total {len(t):3d} ({sum(l['pax'] for l in t):3d} pax) | dropped {len(u):2d} ({sum(l['pax'] for l in u):3d} pax) {100*len(u)/len(t) if t else 0:.0f}%\n")

W("\nCOVERAGE BY TRIP TYPE\n")
tts = sorted({l['trip_type'] for l in legs})
for tt in tts:
    t=[l for l in legs if l['trip_type']==tt]; u=[l for l in t if l['assigned_driver']=='Unassigned']
    W(f"  {tt:18s} total {len(t):3d} ({sum(l['pax'] for l in t):3d} pax) | dropped {len(u):2d} ({sum(l['pax'] for l in u):3d} pax)\n")
cr=[l for l in legs if l['is_cruise']]; cru=[l for l in cr if l['assigned_driver']=='Unassigned']
W(f"  ALL CRUISE         total {len(cr):3d} ({sum(l['pax'] for l in cr):3d} pax) | dropped {len(cru):2d} ({sum(l['pax'] for l in cru):3d} pax)\n")

W("\n"+"="*78+"\nPER-DRIVER CHAINS (sched buffer / realistic slack, min; LATE<0, TIGHT<15)\n"+"="*78+"\n")
for d in sorted(byd, key=lambda d:byd[d][0]['dt']):
    ls = byd[d]
    span = (clear_dt(ls[-1]) - ls[0]['dt']).total_seconds()/3600
    inferred = max(l['tier'] for l in ls)
    native = TIER[DRIVER_TYPE[d]]
    W(f"\n{d} [{DRIVER_TYPE[d]}; today max-tier={inferred}{' << native '+str(native) if inferred<native else ''}] "
      f"{len(ls)} jobs, {ls[0]['dt']:%I:%M%p}->{clear_dt(ls[-1]):%I:%M%p} span {span:.1f}h{'  ** >14h SPAN' if span>14 else ''}\n")
    if d in WINDOW:
        st,en = WINDOW[d]
        if ls[0]['dt'].hour < st: W(f"  ** WINDOW: first pickup {ls[0]['dt']:%I:%M%p} before {st}:00 start\n")
        if clear_dt(ls[-1]) > D0+timedelta(hours=en): W(f"  ** WINDOW: last clear {clear_dt(ls[-1]):%I:%M%p} after {en}:00 end (CLEAR_BY)\n")
    if d in PREV_CLEAR:
        pcl = datetime(2026,6,13,*map(int,PREV_CLEAR[d].split(':')))
        rest = (ls[0]['dt']-pcl).total_seconds()/3600
        if rest < 9: W(f"  ** REST: cleared {pcl:%I:%M%p} prev night -> first pickup {ls[0]['dt']:%I:%M%p} = {rest:.1f}h\n")
    for a,b in zip(ls, ls[1:]):
        s,r = chain_buffers(a,b)
        flag = 'LATE-RISK' if r<0 else ('REJECT?' if s<0 else ('tight' if s<15 else ''))
        if flag:
            W(f"  {flag:9s} {a['dt']:%I:%M%p} {a['trip_type'][:9]:9s} {a['pc'][:4]}->{a['dc'][:4]} clears {clear_dt(a,True):%I:%M%p}"
              f" then {b['dt']:%I:%M%p} {b['trip_type'][:9]:9s} {b['pc'][:4]}->{b['dc'][:4]} | sched {s:+.0f} real {r:+.0f} (legs {a['leg_id']}->{b['leg_id']})\n")

W("\n"+"="*78+"\nUNASSIGNED — INSERTION TEST (could any driver have taken it as-is?)\n"+"="*78+"\n")
def fits(d, L, tier_cap):
    if L['tier'] > tier_cap: return None
    if d in WINDOW:
        st,en = WINDOW[d]
        if L['dt'] < D0+timedelta(hours=st) or clear_dt(L) > D0+timedelta(hours=en): return None
    ls = byd[d]
    prevs = [x for x in ls if x['dt']<=L['dt']]; nxts = [x for x in ls if x['dt']>L['dt']]
    ok_p = ok_n = True; s1=s2=None
    if prevs: s1,_ = chain_buffers(prevs[-1], L); ok_p = s1>=0
    if nxts:  s2,_ = chain_buffers(L, nxts[0]);  ok_n = s2>=0
    return (s1,s2) if ok_p and ok_n else None

for L in unassigned:
    today_ok, native_ok = [], []
    for d in byd:
        cap_today = max(x['tier'] for x in byd[d])
        if fits(d, L, cap_today): today_ok.append(d)
        elif fits(d, L, TIER[DRIVER_TYPE[d]]): native_ok.append(d)
    tag = 'CRUISE ' if L['is_cruise'] else ''
    W(f"{L['dt']:%I:%M%p} {tag}{L['vt']:13s} {L['pax']:2d}pax  {L['pc'][:14]:14s}->{L['dc'][:14]:14s} leg {L['leg_id']}\n")
    W(f"    fits-today: {', '.join(today_ok) if today_ok else 'NOBODY'}")
    if native_ok: W(f" | fits-if-native-vehicle: {', '.join(native_ok)}")
    W("\n")

W("\nSAME-RESERVATION LEGS SPLIT ACROSS DRIVERS\n")
byres = defaultdict(list)
for l in legs: byres[l['reservation_id']].append(l)
for rid, ls in byres.items():
    if len(ls)>1:
        ds = {l['assigned_driver'] for l in ls}
        W(f"  res {rid} ({ls[0]['guest_name']}): " + " | ".join(f"{l['dt']:%I:%M%p} {l['trip_type']} -> {l['assigned_driver']}" for l in sorted(ls,key=lambda x:x['dt'])) + ("   ** SPLIT" if len(ds)>1 else "") + "\n")

W("\nHOURLY: jobs needing a driver vs drivers occupied (rough)\n")
for h in range(0,22):
    t0,t1 = D0+timedelta(hours=h), D0+timedelta(hours=h+1)
    need = [l for l in legs if t0<=l['dt']<t1]
    un = [l for l in need if l['assigned_driver']=='Unassigned']
    busy = {d for d,ls in byd.items() for l in ls if l['dt']<t1 and clear_dt(l,True)>t0}
    if need: W(f"  {h:02d}:00  jobs {len(need):2d} (dropped {len(un):2d})  drivers busy {len(busy):2d}/13\n")
