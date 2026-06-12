# Verify the founder's 5 manual moves against the scheduler timing model
from datetime import datetime, timedelta

D = lambda h, m: datetime(2026, 6, 14, h, m)
DWELL, GRACE = 45, 15
T = {('MCO','DIS'):30, ('DIS','MCO'):30, ('MCO','UNI'):25, ('UNI','MCO'):25,
     ('MCO','PORT'):55, ('PORT','MCO'):55, ('DIS','DIS'):12, ('MCO','MCO'):2,
     ('MCO','AIRH'):12, ('AIRH','MCO'):12, ('AIRH','DIS'):25, ('DIS','AIRH'):25,
     ('PORT','PORT'):10, ('UNI','DIS'):28, ('DIS','UNI'):28}

def clear(pickup, pc, dc, is_arr):
    d = T[(pc, dc)]
    return pickup + timedelta(minutes=(DWELL + d) if is_arr else d)

def buf(prev_clear, prev_dc, nxt_pickup, nxt_pc, nxt_is_arr):
    if nxt_is_arr:
        repo = 0 if prev_dc == 'MCO' else T[(prev_dc, 'MCO')]
        return (nxt_pickup - (prev_clear + timedelta(minutes=repo - GRACE))).total_seconds()/60
    repo = T[(prev_dc, nxt_pc)]
    return (nxt_pickup - (prev_clear + timedelta(minutes=repo))).total_seconds()/60

# (label, prev(pickup,pc,dc,arr), new(pickup,pc,dc,arr), next(pickup,pc,arr))
MOVES = [
 ("M1 ken: drop 9:27 arr(7pax), take 13256 10:30 dep Univ->MCO(6pax)",
  (D(9,0),'DIS','MCO',False), (D(10,30),'UNI','MCO',False), (D(11,10),'MCO',True)),
 ("M2 sereen: drop 9:30 arr(6pax), take 22551 10:00 dep AoA->MCO(4pax)",
  (D(8,30),'DIS','MCO',False), (D(10,0),'DIS','MCO',False), (D(11,45),'DIS',False)),
 ("M3 runer: drop 10:00 arr(4pax), take 22907 11:00 dep AKL->MCO(2pax)",
  (D(9,30),'DIS','MCO',False), (D(11,0),'DIS','MCO',False), (D(11,30),'MCO',True)),
 ("M4 Aftab: drop 10:15 arr(4pax), take 11:00 TC dep ->MCO(2pax)",
  (D(8,30),'MCO','DIS',True), (D(11,0),'DIS','MCO',False), (D(12,15),'DIS',False)),
 ("M5 Raymond: swap 20100 10:00 Van7 for 13398 10:00 V14-13pax Saratoga->MCO",
  (D(8,0),'DIS','MCO',False), (D(10,0),'DIS','MCO',False), (D(11,0),'AIRH',False)),
]
for label, p, n, x in MOVES:
    pcl = clear(p[0], p[1], p[2], p[3])
    b1 = buf(pcl, p[2], n[0], n[1], n[3])
    ncl = clear(n[0], n[1], n[2], n[3])
    b2 = buf(ncl, n[2], x[0], x[1], x[2])
    ok = "FEASIBLE" if b1 >= 0 and b2 >= 0 else "FAILS"
    print(f"{ok:9s} {label}\n          in-buffer {b1:+.0f} min, out-buffer {b2:+.0f} min "
          f"(prev clears {pcl:%I:%M%p}, new clears {ncl:%I:%M%p})")
