#!/usr/bin/env python
"""29 — What the task queue files, what closes it, and what a dispatcher's click bought.

THE QUESTION THIS ANSWERS
-------------------------
Founder, 2026-09-21: "I want the tasks to be worked on, not looked at." For every
OperationalTask type: how many are filed a day, how each one actually ends, whether a
person ever touched it, how often a hand-close is re-filed within a day, and whether the
thing the task was about (a driver move, a retime, a payment, a fee) ever happened.

Read-only over the snapshot. Three passes, printed in order:
  1. volume by month; last-30-day filing/closure/human-touch table; 90-day closure
     families per type; re-filing depth; the open backlog by trip date
  2. scanner types split same-day vs future-board; the hand-close→re-file loop;
     flight re-filing and whether the pickup ever moved; after-hours fee billing;
     payment chasing vs paying on its own; manual tasks; snoozes; who closes
  3. flight tasks by days-out and drift size; conflicts by projected lateness and
     how fast the self-resolving ones go; the daily click bill

Baseline (snapshot 2026-09-21, 60 days): ~200 tasks/day filed, ~120 clicks/day;
half of same-day tight turns gone within one 30-min tick; 38% / 49% / 94% of hand-closed
turn / flight / fee tasks re-filed within a day; 0 of 321 future-board tight turns led to
a move; 88% of fee tasks ended with no fee on the leg. The rules those numbers changed
live in ops/tasks.py (TURN_CONFIRM_*, HAND_DISMISS_HOURS, PAYMENT_CHASE_DAYS_AHEAD) and
ops/views.py (_came_back_count). Re-run this after a fresh pull to see whether they held.

USAGE
  GRAYSON_SNAPSHOT_DB=content/db.sqlite3 python docs/scheduling-redesign/analysis/29_task_queue_audit.py
"""
import os
DB = os.environ.get("GRAYSON_SNAPSHOT_DB", "content/db.sqlite3")


# ═══════════════════════════ PASS 1 ═══════════════════════════
import sqlite3, json, statistics as st, datetime as dt, re
from collections import Counter, defaultdict
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
def q(sql, *a): return con.execute(sql, a).fetchall()
def p(s=""): print(s)
def pct(n, d): return f"{100*n/d:5.1f}%" if d else "   — "
def parse(ts):
    if not ts: return None
    ts = ts.replace("Z","")
    try: return dt.datetime.fromisoformat(ts)
    except: return dt.datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")

lo, hi = q("select min(created_at) lo, max(created_at) hi from ops_operationaltask")[0]
p(f"tasks span {lo} → {hi}")
now = parse(hi)
cut30 = (now - dt.timedelta(days=30)).isoformat(sep=" ")
cut90 = (now - dt.timedelta(days=90)).isoformat(sep=" ")

p("\n=== VOLUME per type, per month (filed) ===")
rows = q("select task_type, substr(created_at,1,7) m, count(*) c from ops_operationaltask group by 1,2 order by 1,2")
by = defaultdict(dict)
for r in rows: by[r["task_type"]][r["m"]] = r["c"]
months = sorted({r["m"] for r in rows})[-5:]
p(f"{'type':22}" + "".join(f"{m:>9}" for m in months))
for t, d in sorted(by.items(), key=lambda kv: -sum(kv[1].values())):
    p(f"{t:22}" + "".join(f"{d.get(m,0):>9}" for m in months))

p("\n=== LAST 30 DAYS: filed/day, how closed, human touch, time open ===")
tasks = q("""select t.*, l.pickup_date, l.pickup_time, l.driver_id from ops_operationaltask t
             left join reservations_leg l on l.id=t.leg_id where t.created_at >= ?""", cut30)
claimed = {r["task_id"] for r in q("select distinct task_id from ops_staffactivity where action_type in ('task_claimed','task_assigned') and task_id is not null")}
completed_by_human = {r["task_id"] for r in q("select distinct task_id from ops_staffactivity where action_type='task_completed' and task_id is not null")}
snoozed = {r["task_id"] for r in q("select distinct task_id from ops_staffactivity where action_type='task_snoozed' and task_id is not null")}
commed = {r["task_id"] for r in q("select distinct task_id from ops_staffactivity where action_type='comm_logged' and task_id is not null")}
days = 30
bytype = defaultdict(list)
for t in tasks: bytype[t["task_type"]].append(t)
hdr = f"{'type':20}{'filed':>6}{'/day':>6}{'open':>6}{'auto':>7}{'human':>7}{'blank':>7}{'cancel':>7}{'claim':>7}{'snooze':>7}{'comm':>6}{'P50 open':>10}{'P75':>7}"
p(hdr)
for tt, ts in sorted(bytype.items(), key=lambda kv: -len(kv[1])):
    n = len(ts)
    opn = sum(1 for t in ts if t["status"] in ("pending","in_progress","snoozed","escalated"))
    closed = [t for t in ts if t["status"] in ("completed","cancelled")]
    auto = sum(1 for t in closed if t["resolved_by_id"] is None and t["status"]=="completed")
    human = sum(1 for t in closed if t["resolved_by_id"] is not None)
    blank = sum(1 for t in closed if t["resolved_by_id"] is not None and not (t["resolution_notes"] or "").strip())
    canc = sum(1 for t in closed if t["status"]=="cancelled")
    cl = sum(1 for t in ts if t["id"] in claimed or t["assigned_to_id"])
    sn = sum(1 for t in ts if t["id"] in snoozed)
    cm = sum(1 for t in ts if t["id"] in commed)
    opens = [ (parse(t["resolved_at"])-parse(t["created_at"])).total_seconds()/60 for t in closed if t["resolved_at"]]
    p50 = f"{st.median(opens):.0f}m" if opens else "—"
    p75 = f"{sorted(opens)[int(len(opens)*.75)]:.0f}m" if opens else "—"
    p(f"{tt:20}{n:>6}{n/days:>6.1f}{opn:>6}{pct(auto,len(closed)):>7}{pct(human,len(closed)):>7}{pct(blank,len(closed)):>7}{pct(canc,len(closed)):>7}{pct(cl,n):>7}{pct(sn,n):>7}{pct(cm,n):>6}{p50:>10}{p75:>7}")
p("(auto/human/blank/cancel are shares of CLOSED tasks; claim/snooze/comm are shares of all filed)")

p("\n=== LAST 90 DAYS: how each type actually closes (resolution note families) ===")
def fam(note, resolved_by, status):
    n = (note or "").strip()
    if status == "cancelled": return "cancelled: " + (n[:40] or "(no reason)")
    if resolved_by is not None:
        return "HAND: " + (n[:45] if n else "(blank note)")
    n = re.sub(r"\d+", "#", n)
    return "auto: " + n[:60]
rows = q("select task_type, status, resolved_by_id, resolution_notes from ops_operationaltask where created_at >= ? and status in ('completed','cancelled')", cut90)
fams = defaultdict(Counter)
for r in rows: fams[r["task_type"]][fam(r["resolution_notes"], r["resolved_by_id"], r["status"])] += 1
for tt, c in sorted(fams.items(), key=lambda kv: -sum(kv[1].values())):
    tot = sum(c.values())
    p(f"\n-- {tt}  ({tot} closed)")
    for k, v in c.most_common(9):
        p(f"   {pct(v,tot)}  {v:>5}  {k}")
    rest = tot - sum(v for _, v in c.most_common(9))
    if rest: p(f"   {pct(rest,tot)}  {rest:>5}  (other)")

p("\n=== HAND-CLOSED: what notes people actually write (last 90d, all types) ===")
rows = q("select task_type, resolution_notes from ops_operationaltask where created_at >= ? and resolved_by_id is not null", cut90)
c = Counter((r["task_type"], (r["resolution_notes"] or "").strip().lower()[:50]) for r in rows)
for (tt, n), v in c.most_common(25): p(f"  {v:>5}  {tt:18} {n or '(blank)'}")

p("\n=== RE-FILING: same object + type filed more than once (last 90d) ===")
rows = q("""select task_type, coalesce('leg:'||leg_id, 'res:'||reservation_id, 'lead:'||lead_id, 'cf:'||contact_form_id, 'none') k, count(*) c
            from ops_operationaltask where created_at >= ? group by 1,2""", cut90)
agg = defaultdict(lambda: [0,0,0])  # objects, tasks, tasks beyond first
for r in rows:
    a = agg[r["task_type"]]; a[0]+=1; a[1]+=r["c"]; a[2]+=r["c"]-1
p(f"{'type':20}{'objects':>9}{'tasks':>8}{'re-filed':>10}{'share':>8}{'max/obj':>9}")
mx = {r["task_type"]: 0 for r in rows}
for r in rows: mx[r["task_type"]] = max(mx[r["task_type"]], r["c"])
for tt, (o, t, extra) in sorted(agg.items(), key=lambda kv: -kv[1][2]):
    p(f"{tt:20}{o:>9}{t:>8}{extra:>10}{pct(extra,t):>8}{mx[tt]:>9}")

p("\n=== OPEN RIGHT NOW ===")
rows = q("""select t.task_type, t.status, t.priority, t.created_at, t.due_at, t.assigned_to_id, t.snoozed_until, l.pickup_date
            from ops_operationaltask t left join reservations_leg l on l.id=t.leg_id
            where t.status in ('pending','in_progress','snoozed','escalated')""")
today = now.date()
c = defaultdict(lambda: Counter())
for r in rows:
    age = (now - parse(r["created_at"])).days
    ld = dt.date.fromisoformat(r["pickup_date"]) if r["pickup_date"] else None
    c[r["task_type"]]["open"] += 1
    c[r["task_type"]]["claimed"] += 1 if r["assigned_to_id"] else 0
    c[r["task_type"]]["snoozed"] += 1 if r["status"]=="snoozed" else 0
    c[r["task_type"]]["age>7d"] += 1 if age > 7 else 0
    c[r["task_type"]]["leg in past"] += 1 if ld and ld < today else 0
    c[r["task_type"]]["leg today"] += 1 if ld and ld == today else 0
    c[r["task_type"]]["leg future"] += 1 if ld and ld > today else 0
    c[r["task_type"]]["no leg"] += 1 if not ld else 0
keys = ["open","claimed","snoozed","age>7d","leg in past","leg today","leg future","no leg"]
p(f"{'type':20}" + "".join(f"{k:>12}" for k in keys))
for tt, cc in sorted(c.items(), key=lambda kv: -kv[1]["open"]):
    p(f"{tt:20}" + "".join(f"{cc[k]:>12}" for k in keys))
p(f"snapshot 'now' = {now}  today = {today}")


# ═══════════════════════════ PASS 2 ═══════════════════════════
import sqlite3, json, statistics as st, datetime as dt, re
from collections import Counter, defaultdict
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); con.row_factory = sqlite3.Row
def q(sql, *a): return con.execute(sql, a).fetchall()
def p(s=""): print(s)
def pct(n, d): return f"{100*n/d:5.1f}%" if d else "   — "
def parse(ts):
    if not ts: return None
    try: return dt.datetime.fromisoformat(ts.replace("Z",""))
    except: return dt.datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
now = parse(q("select max(created_at) m from ops_operationaltask")[0]["m"])
cut = (now - dt.timedelta(days=60)).isoformat(sep=" ")

def outcome(note, rb, status):
    n = (note or "").lower()
    if status == "cancelled": return "cancelled"
    if "reassigned" in n or "unassigned" in n: return "MOVED (driver changed)"
    if "flight matched" in n or "pickup updated" in n: return "RETIMED"
    if "escalated" in n: return "escalated (counted twice)"
    if "no longer tight" in n or "conflict resolved" in n or "mismatch resolved" in n: return "self-resolved (arithmetic changed)"
    if "completed" in n or "has passed" in n or "cancelled" in n: return "expired (leg ran / date passed)"
    if rb is not None and not n.strip(): return "hand-closed, blank"
    if rb is not None: return "hand-closed, with note"
    return "other auto: " + n[:30]

p("=== SCANNER TYPES, last 60d: outcome split, same-day vs future-board ===")
rows = q("select task_type, status, resolved_by_id, resolution_notes, metadata, created_at, resolved_at, leg_id from ops_operationaltask where created_at>=? and task_type in ('driver_conflict','tight_turn') and status in ('completed','cancelled')", cut)
split = defaultdict(Counter)
for r in rows:
    m = json.loads(r["metadata"] or "{}")
    horizon = "future-board" if m.get("future_board") or (m.get("days_until") or 0) > 0 else "same-day"
    split[(r["task_type"], horizon)][outcome(r["resolution_notes"], r["resolved_by_id"], r["status"])] += 1
for k in sorted(split):
    c = split[k]; tot = sum(c.values())
    p(f"\n-- {k[0]} / {k[1]}  ({tot} closed)")
    for o, v in c.most_common(): p(f"   {pct(v,tot)} {v:>5}  {o}")

p("\n=== THE LOOP: hand-closed blank, then the same leg re-filed (same type) within 24h ===")
tasks = q("select id, task_type, leg_id, created_at, resolved_at, resolved_by_id, resolution_notes from ops_operationaltask where created_at>=? and leg_id is not null and task_type in ('driver_conflict','tight_turn','flight_verify','afterhours_fee') order by created_at", cut)
byleg = defaultdict(list)
for t in tasks: byleg[(t["task_type"], t["leg_id"])].append(t)
for tt in ("tight_turn","driver_conflict","flight_verify","afterhours_fee"):
    hand = came_back = 0; gaps = []
    for (typ, leg), ts in byleg.items():
        if typ != tt: continue
        for i, t in enumerate(ts):
            if t["resolved_by_id"] is None or (t["resolution_notes"] or "").strip(): continue
            hand += 1
            nxt = [u for u in ts[i+1:] if parse(u["created_at"]) - parse(t["resolved_at"]) <= dt.timedelta(hours=24)]
            if nxt:
                came_back += 1; gaps.append((parse(nxt[0]["created_at"]) - parse(t["resolved_at"])).total_seconds()/60)
    med = f"{st.median(gaps):.0f}m" if gaps else "—"
    p(f"  {tt:16} hand-closed blank: {hand:>5}   came back <24h: {came_back:>5} ({pct(came_back,hand)})   median gap {med}")

p("\n=== flight_verify: re-filing depth per leg and whether the pickup ever moved ===")
rows = q("select leg_id, count(*) c from ops_operationaltask where created_at>=? and task_type='flight_verify' group by 1", cut)
depth = Counter(min(r["c"], 6) for r in rows)
p("  tasks per leg: " + ", ".join(f"{k if k<6 else '6+'}:{v}" for k, v in sorted(depth.items())))
heavy = [r["leg_id"] for r in rows if r["c"] >= 4]
p(f"  legs with 4+ flight tasks: {len(heavy)} → {sum(r['c'] for r in rows if r['c']>=4)} tasks")
# did the leg's pickup time change while a flight task was open?
moved = same = 0
for r in q("select t.leg_id, t.created_at, t.resolved_at from ops_operationaltask t where t.created_at>=? and t.task_type='flight_verify' and t.status='completed' and t.resolution_notes like '%mismatch resolved%'", cut):
    h = q("select pickup_time from reservations_historicalleg where id=? and history_date between ? and ? order by history_date", r["leg_id"], r["created_at"], r["resolved_at"] or now.isoformat(sep=" "))
    before = q("select pickup_time from reservations_historicalleg where id=? and history_date<=? order by history_date desc limit 1", r["leg_id"], r["created_at"])
    times = {x["pickup_time"] for x in h} | ({before[0]["pickup_time"]} if before else set())
    if len(times) > 1: moved += 1
    else: same += 1
p(f"  'mismatch resolved' closes: pickup time changed during the task: {moved}   unchanged (flight drifted back / matched to same minute): {same}")

p("\n=== afterhours_fee: was the $20 actually billed? (60d) ===")
rows = q("""select t.status, t.resolved_by_id, t.resolution_notes, l.afterhours_fee, l.pickup_date, l.status ls
            from ops_operationaltask t join reservations_leg l on l.id=t.leg_id where t.created_at>=? and t.task_type='afterhours_fee'""", cut)
c = Counter()
for r in rows:
    o = outcome(r["resolution_notes"], r["resolved_by_id"], r["status"]) if r["status"] in ("completed","cancelled") else "still open"
    billed = "fee on leg" if (r["afterhours_fee"] or 0) > 0 else "NO fee on leg"
    c[(o, billed)] += 1
tot = sum(c.values())
for (o, b), v in sorted(c.items(), key=lambda kv: -kv[1]): p(f"   {pct(v,tot)} {v:>4}  {o:40} {b}")

p("\n=== payment_chase: does chasing change when they pay? (60d) ===")
rows = q("""select t.id, t.reservation_id, t.status, t.resolved_by_id, t.resolution_notes, t.created_at, t.resolved_at,
            (select count(*) from ops_staffactivity a where a.task_id=t.id and a.action_type in ('comm_logged','task_claimed')) touched,
            (select count(*) from ops_staffactivity a where a.task_id=t.id and a.action_type='task_snoozed') snoozes
            from ops_operationaltask t where t.created_at>=? and t.task_type='payment_chase'""", cut)
g = defaultdict(list)
for r in rows:
    o = outcome(r["resolution_notes"], r["resolved_by_id"], r["status"]) if r["status"] in ("completed","cancelled") else "still open"
    if r["resolution_notes"] and "payment received" in r["resolution_notes"].lower(): o = "paid (auto)"
    key = "touched by a person" if r["touched"] else "never touched"
    hrs = (parse(r["resolved_at"]) - parse(r["created_at"])).total_seconds()/3600 if r["resolved_at"] else None
    g[(key, o)].append(hrs)
for (k, o), hs in sorted(g.items(), key=lambda kv: -len(kv[1])):
    hs2 = [h for h in hs if h is not None]
    p(f"   {len(hs):>4}  {k:20} {o:32} median open {st.median(hs2):.0f}h" if hs2 else f"   {len(hs):>4}  {k:20} {o}")
p(f"  snoozed at least once: {sum(1 for r in rows if r['snoozes'])} of {len(rows)}; total snoozes {sum(r['snoozes'] for r in rows)}")
p("  the 27 open now — trip date vs today:")
for r in q("""select t.id, t.created_at, r.id rid, (select min(pickup_date) from reservations_leg l where l.reservation_id=r.id and l.status not in ('cancelled')) trip, r.status rs,
              (select count(*) from ops_staffactivity a where a.task_id=t.id and a.action_type='task_snoozed') sn
              from ops_operationaltask t join reservations_reservation r on r.id=t.reservation_id where t.task_type='payment_chase' and t.status in ('pending','in_progress','snoozed','escalated') order by trip"""):
    p(f"     task {r['id']}  R{r['rid']}  filed {r['created_at'][:10]}  trip {r['trip']}  res status {r['rs']}  snoozes {r['sn']}")

p("\n=== manual tasks: who files them and what they are (60d) ===")
rows = q("select created_by_id, title from ops_operationaltask where created_at>=? and task_type='manual'", cut)
p(f"  created_by null (system): {sum(1 for r in rows if r['created_by_id'] is None)}  by a person: {sum(1 for r in rows if r['created_by_id'])}")
c = Counter(re.sub(r"[A-Z][a-z]+ [A-Z][a-z]+|\d+", "…", r["title"])[:60] for r in rows)
for k, v in c.most_common(8): p(f"   {v:>4}  {k}")

p("\n=== confirmation_texts: what is this task? ===")
for r in q("select title, substr(description,1,160) d, created_at from ops_operationaltask where task_type='confirmation_texts' order by id desc limit 2"): p(f"   {r['created_at'][:16]}  {r['title']} — {r['d']}")

p("\n=== snoozes → what happened next (60d, all types) ===")
rows = q("""select t.task_type, t.status, t.resolved_by_id, t.resolution_notes from ops_operationaltask t
            where t.created_at>=? and exists(select 1 from ops_staffactivity a where a.task_id=t.id and a.action_type='task_snoozed')""", cut)
c = Counter((r["task_type"], outcome(r["resolution_notes"], r["resolved_by_id"], r["status"]) if r["status"] in ("completed","cancelled") else "still open") for r in rows)
for (tt, o), v in c.most_common(10): p(f"   {v:>4}  {tt:16} {o}")

p("\n=== who closes by hand (30d, task_completed activity) ===")
rows = q("""select u.first_name, u.username, t.task_type, count(*) c from ops_staffactivity a join auth_user u on u.id=a.user_id join ops_operationaltask t on t.id=a.task_id
            where a.action_type='task_completed' and a.created_at>=? group by 1,2,3 order by 4 desc""", (now - dt.timedelta(days=30)).isoformat(sep=" "))
per = defaultdict(Counter)
for r in rows: per[r["first_name"] or r["username"]][r["task_type"]] += r["c"]
for who, c in sorted(per.items(), key=lambda kv: -sum(kv[1].values())):
    p(f"   {who:12} {sum(c.values()):>5}  " + ", ".join(f"{k} {v}" for k, v in c.most_common(4)))


# ═══════════════════════════ PASS 3 ═══════════════════════════
import sqlite3, json, statistics as st, datetime as dt
from collections import Counter, defaultdict
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); con.row_factory = sqlite3.Row
def q(sql, *a): return con.execute(sql, a).fetchall()
def p(s=""): print(s)
def pct(n, d): return f"{100*n/d:5.1f}%" if d else "   — "
def parse(ts):
    try: return dt.datetime.fromisoformat(ts.replace("Z",""))
    except: return dt.datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
now = parse(q("select max(created_at) m from ops_operationaltask")[0]["m"])
cut = (now - dt.timedelta(days=60)).isoformat(sep=" ")
def outcome(note, rb, status):
    n = (note or "").lower()
    if status == "cancelled": return "cancelled"
    if "reassigned" in n or "unassigned" in n: return "MOVED"
    if "flight matched" in n or "pickup updated" in n: return "RETIMED"
    if "escalated" in n: return "escalated"
    if "no longer tight" in n or "conflict resolved" in n or "mismatch resolved" in n: return "self-resolved"
    if "completed" in n or "has passed" in n or "cancelled" in n: return "expired"
    if rb is not None and not n.strip(): return "hand-blank"
    return "other"

p("=== flight_verify (60d): outcome by days-out at filing, and by drift size ===")
rows = q("select status, resolved_by_id, resolution_notes, metadata, created_at, resolved_at from ops_operationaltask where created_at>=? and task_type='flight_verify' and status in ('completed','cancelled')", cut)
byd = defaultdict(Counter); bym = defaultdict(Counter); opn = defaultdict(list)
for r in rows:
    m = json.loads(r["metadata"] or "{}"); o = outcome(r["resolution_notes"], r["resolved_by_id"], r["status"])
    d = m.get("days_until_pickup"); d = "?" if d is None else ("0" if d == 0 else "1" if d == 1 else "2-3" if d <= 3 else "4-7")
    mm = abs(m.get("mismatch_minutes") or 0); mb = "<30" if mm < 30 else "30-59" if mm < 60 else "60-119" if mm < 120 else "120+"
    byd[d][o] += 1; bym[mb][o] += 1
    if r["resolved_at"]: opn[o].append((parse(r["resolved_at"])-parse(r["created_at"])).total_seconds()/60)
keys = ["self-resolved","RETIMED","hand-blank","expired","cancelled","other"]
p(f"{'days out':>9}{'n':>6}" + "".join(f"{k:>15}" for k in keys))
for d in ["0","1","2-3","4-7","?"]:
    c = byd[d]; t = sum(c.values())
    if t: p(f"{d:>9}{t:>6}" + "".join(f"{pct(c[k],t):>15}" for k in keys))
p(f"{'drift':>9}{'n':>6}" + "".join(f"{k:>15}" for k in keys))
for mb in ["<30","30-59","60-119","120+"]:
    c = bym[mb]; t = sum(c.values())
    if t: p(f"{mb:>9}{t:>6}" + "".join(f"{pct(c[k],t):>15}" for k in keys))
p("  minutes open, P50 by outcome: " + ", ".join(f"{k} {st.median(v):.0f}m" for k, v in opn.items() if v))

p("\n=== same-day driver_conflict (60d): outcome by how late the driver was projected ===")
rows = q("select status, resolved_by_id, resolution_notes, metadata, created_at, resolved_at from ops_operationaltask where created_at>=? and task_type='driver_conflict' and status in ('completed','cancelled')", cut)
byl = defaultdict(Counter); opn = defaultdict(list)
for r in rows:
    m = json.loads(r["metadata"] or "{}")
    if m.get("future_board") or (m.get("days_until") or 0) > 0: continue
    late = m.get("conflict_minutes") or m.get("mismatch_minutes") or 0
    lb = "11-20" if late <= 20 else "21-40" if late <= 40 else "41-90" if late <= 90 else "90+"
    o = outcome(r["resolution_notes"], r["resolved_by_id"], r["status"]); byl[lb][o] += 1
    if r["resolved_at"]: opn[o].append((parse(r["resolved_at"])-parse(r["created_at"])).total_seconds()/60)
keys = ["MOVED","RETIMED","self-resolved","expired","hand-blank","cancelled"]
p(f"{'min late':>9}{'n':>6}" + "".join(f"{k:>15}" for k in keys))
for lb in ["11-20","21-40","41-90","90+"]:
    c = byl[lb]; t = sum(c.values())
    if t: p(f"{lb:>9}{t:>6}" + "".join(f"{pct(c[k],t):>15}" for k in keys))
p("  minutes open, P50 by outcome: " + ", ".join(f"{k} {st.median(v):.0f}m" for k, v in opn.items() if v))
fast = sum(1 for v in opn["self-resolved"] if v <= 35); p(f"  self-resolved within one scan tick (≤35 min): {fast} of {len(opn['self-resolved'])} ({pct(fast, len(opn['self-resolved']))})")

p("\n=== same-day tight_turn (60d): how fast the self-resolving ones go away ===")
rows = q("select status, resolved_by_id, resolution_notes, metadata, created_at, resolved_at from ops_operationaltask where created_at>=? and task_type='tight_turn' and status='completed'", cut)
opn = defaultdict(list)
for r in rows:
    m = json.loads(r["metadata"] or "{}")
    if m.get("future_board") or (m.get("days_until") or 0) > 0: continue
    o = outcome(r["resolution_notes"], r["resolved_by_id"], r["status"])
    if r["resolved_at"]: opn[o].append((parse(r["resolved_at"])-parse(r["created_at"])).total_seconds()/60)
for o, v in sorted(opn.items(), key=lambda kv: -len(kv[1])):
    p(f"   {o:14} n={len(v):>4}  P50 {st.median(v):.0f}m  ≤35m: {pct(sum(1 for x in v if x<=35), len(v))}")
lates = Counter()
for r in rows:
    m = json.loads(r["metadata"] or "{}"); lm = m.get("late_minutes") or m.get("conflict_minutes")
    if lm is not None: lates["1-3" if lm <= 3 else "4-6" if lm <= 6 else "7-10" if lm <= 10 else "11+"] += 1
p("   late-minutes at filing: " + ", ".join(f"{k}: {v}" for k, v in sorted(lates.items())))

p("\n=== the daily bill: human clicks on tasks per day (30d) ===")
rows = q("select action_type, t.task_type, count(*) c from ops_staffactivity a join ops_operationaltask t on t.id=a.task_id where a.created_at>=? and a.action_type in ('task_completed','task_claimed','task_snoozed','comm_logged') group by 1,2", (now-dt.timedelta(days=30)).isoformat(sep=" "))
tot = Counter(); by = defaultdict(int)
for r in rows: tot[r["action_type"]] += r["c"]; by[r["task_type"]] += r["c"]
p("   " + ", ".join(f"{k} {v/30:.0f}/day" for k, v in tot.most_common()))
p("   by type: " + ", ".join(f"{k} {v/30:.0f}/day" for k, v in sorted(by.items(), key=lambda kv: -kv[1])))
