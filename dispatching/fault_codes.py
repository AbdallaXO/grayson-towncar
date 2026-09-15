"""
What a fault code means, for someone who is not a mechanic.

The Fleet desk tells the fleet manager to "read them before the next
assignment", and what he reads is the string the car's own ECU emits:

    P202E — Reductant Injection Valve Circuit Range/Performance Bank 1 Unit 1

That is an unfollowable instruction. He cannot tell from it whether a guest can
get in the car this morning, which is the only question he is actually asking.
This module answers that question in English.

THE VERDICT IS ADVICE, NEVER A GATE
────────────────────────────────────────────────────────────────────────────
Nothing here removes a car from anything. Only the human-entered downtime ledger
does that, and it is permitted to precisely because a person set it by hand —
the founder's standing ruling, recorded in ``fleet_health`` and ``day_setup``,
after an automatic readiness gate (Guard A) was built and pulled for false
positives. A ``HOLD`` verdict below is a strongly worded sentence on a screen.
If the car must not go out, somebody takes it off the road.

Two sources, deliberately ranked in this order:

  1. KNOWN — an explicit entry for a code, written for this fleet's vehicles.
     Every code the fleet has actually thrown is in here, plus the common
     neighbours of each family, because a fault arrives at 6 AM and the answer
     has to already exist.
  2. FAMILY — a structural read of the code itself when it is not in the table.
     OBD-II codes are systematic: the letter is the system and the digits narrow
     it, so an unrecognised P03xx is still confidently "an ignition misfire".
     Vaguer, and says so.

Never invented: if neither source is confident, the module says it does not know
and shows the car's own description. "I don't know, ring the shop" is a better
morning than a confident wrong answer about a car carrying guests.
"""
from __future__ import annotations

import re

# ── The three answers to "can it go out?" ───────────────────────────────────
DRIVE = "drive"   # Fine to run. Deal with it whenever.
BOOK = "book"     # It will run, but it needs the shop — book a window.
HOLD = "hold"     # Do not put a guest in it until someone looks.

VERDICT_LABEL = {
    DRIVE: "Fine to run",
    BOOK: "Runs — book the shop",
    HOLD: "Don't send it out",
}
VERDICT_TONE = {DRIVE: "good", BOOK: "caution", HOLD: "critical"}
VERDICT_RANK = {HOLD: 0, BOOK: 1, DRIVE: 2}


def _m(system, plain, consequence, verdict, note=""):
    return {"system": system, "plain": plain, "consequence": consequence,
            "verdict": verdict, "note": note, "confident": True}


# ════════════════════════════════════════════════════════════════════════════
# KNOWN CODES — edit this table freely; it is plain data.
#
# Written for a chauffeur fleet: the consequence is always phrased as what it
# means for a car with guests in it today, not what it means to a technician.
# ════════════════════════════════════════════════════════════════════════════

_DEF_SYSTEM = "Diesel exhaust fluid (DEF)"
_DEF_CONSEQ = ("The engine runs normally for now. Left alone the van will lose "
               "power on its own and refuse to restart, and it cannot pass "
               "emissions in this state.")
_DEF_NOTE = ("These arrive in clusters — one DEF fault usually brings three or "
             "four. That is one problem, not four.")

KNOWN = {
    # ── Diesel emissions / DEF. Every Sprinter fault this fleet has seen. ──
    "P202E": _m(_DEF_SYSTEM, "The exhaust fluid injector isn't dosing correctly.",
                _DEF_CONSEQ, BOOK, _DEF_NOTE),
    "P208E": _m(_DEF_SYSTEM, "The exhaust fluid injector is stuck shut.",
                _DEF_CONSEQ, BOOK, _DEF_NOTE),
    "P20EA": _m(_DEF_SYSTEM, "The exhaust fluid control module has lost power.",
                _DEF_CONSEQ, BOOK, _DEF_NOTE),
    "P20F4": _m(_DEF_SYSTEM, "The van is using less exhaust fluid than it should.",
                _DEF_CONSEQ, BOOK, _DEF_NOTE),
    "P204F": _m(_DEF_SYSTEM, "Exhaust fluid quality or level is out of range.",
                _DEF_CONSEQ, BOOK, "Check the DEF tank before booking — it may just need filling."),
    "P2BAD": _m(_DEF_SYSTEM, "The van is emitting more NOx than allowed.",
                _DEF_CONSEQ, BOOK, _DEF_NOTE),

    # ── Catalytic converter ──────────────────────────────────────────────
    "P0420": _m("Catalytic converter",
                "The catalytic converter isn't cleaning the exhaust as well as it should.",
                "Drives and feels completely normal. It will fail an emissions test, "
                "and the repair is expensive enough to plan rather than rush.",
                BOOK,
                "Often follows a misfire that was left too long — worth checking whether "
                "this car has had one."),
    "P0430": _m("Catalytic converter",
                "The catalytic converter on the second bank isn't cleaning the exhaust properly.",
                "Drives normally. Fails emissions.", BOOK),

    # ── Misfires. The family that can become a HOLD. ──────────────────────
    "P0300": _m("Engine misfire",
                "The engine is misfiring, and it isn't confined to one cylinder.",
                "The car may feel rough or hesitant, especially pulling away. A misfire "
                "left running destroys the catalytic converter, which turns a cheap job "
                "into an expensive one.",
                HOLD,
                "Random misfires across cylinders are the worse kind. Don't put a guest "
                "in it until someone has looked."),

    # ── Turbo / boost ────────────────────────────────────────────────────
    "P0299": _m("Turbocharger (low boost)",
                "The turbo isn't producing the boost the engine expects.",
                "The van is noticeably down on power and may drop into limp mode without "
                "warning. Loaded, uphill, or merging onto I-4 is exactly where that bites.",
                HOLD,
                "The car reports this one as serious. A Sprinter full of guests that "
                "cannot accelerate onto a highway is the case to avoid."),
    "P0234": _m("Turbocharger (overboost)",
                "The turbo is producing more boost than the engine wants.",
                "The engine will pull power back to protect itself, so it feels unpredictable.",
                HOLD),

    # ── Things that are genuinely fine to keep running ───────────────────
    "P0455": _m("Fuel vapour system (EVAP)",
                "A large leak in the fuel vapour system — most often a loose or failed fuel cap.",
                "No effect on how the car drives at all. It will fail emissions.",
                DRIVE,
                "Check the fuel cap is clicked shut. That fixes it more often than not."),
    "P0442": _m("Fuel vapour system (EVAP)",
                "A small leak in the fuel vapour system.",
                "No effect on how the car drives. Fails emissions.", DRIVE,
                "Start with the fuel cap."),
    "P0446": _m("Fuel vapour system (EVAP)",
                "A fault in the fuel vapour vent valve.",
                "No effect on how the car drives. Fails emissions.", DRIVE),
    "P0128": _m("Engine cooling",
                "The engine isn't reaching its normal running temperature.",
                "Drives fine. Uses more fuel, and the cabin heater will be weak — which a "
                "guest does notice in January.",
                BOOK),
}

# Cylinder misfires: P0301–P0312 are "cylinder N", and the pattern is regular
# enough to generate rather than type out wrong.
for _n in range(1, 13):
    KNOWN[f"P03{_n:02d}"] = _m(
        "Engine misfire",
        f"Cylinder {_n} is misfiring.",
        "The engine may feel rough or shake at idle. A misfire left running will "
        "destroy the catalytic converter, so this gets worse and more expensive "
        "the longer it waits.",
        BOOK,
        "If the check-engine light is FLASHING rather than steady, don't send the "
        "car out at all — that means it is damaging the converter right now.",
    )


# ════════════════════════════════════════════════════════════════════════════
# FAMILY FALLBACK — for a code the table has never seen
# ════════════════════════════════════════════════════════════════════════════

_CODE_RE = re.compile(r"^([PBCU])([0-3])([0-9A-F])([0-9A-F]{2})$", re.I)

_LETTER = {
    "P": "Engine or transmission",
    "B": "Body electrics",
    "C": "Brakes, steering or suspension",
    "U": "Wiring or a silent module",
}

# Third character of a P-code: the subsystem.
_P_FAMILY = {
    "0": ("Fuel and air metering", BOOK),
    "1": ("Fuel and air metering", BOOK),
    "2": ("Fuel injection", BOOK),
    "3": ("Ignition or misfire", HOLD),
    "4": ("Emissions control", BOOK),
    "5": ("Idle and cruise control", BOOK),
    "6": ("The engine computer", BOOK),
    "7": ("Transmission", HOLD),
    "8": ("Transmission", HOLD),
}


def family(code):
    """A structural read of an unknown code. Honest about being vague."""
    match = _CODE_RE.match((code or "").strip())
    if not match:
        return None
    letter, _kind, third, _rest = (g.upper() for g in match.groups())
    system = _LETTER.get(letter)
    if system is None:
        return None
    if letter == "P":
        name, verdict = _P_FAMILY.get(third, ("Engine or transmission", BOOK))
    elif letter == "C":
        name, verdict = "Brakes, steering or suspension", HOLD
    elif letter == "B":
        name, verdict = "Body electrics", BOOK
    else:
        name, verdict = "Wiring or a silent module", BOOK
    return {
        "system": name,
        "plain": "",
        "consequence": "",
        "verdict": verdict,
        "note": "",
        "confident": False,
    }


# ════════════════════════════════════════════════════════════════════════════
# The one function the rest of the codebase calls
# ════════════════════════════════════════════════════════════════════════════

def explain(code, description="", severity=""):
    """What this code means, and whether the car can go out.

    ``description`` is the car's own ECU string, kept as the fallback so an
    unknown code still shows something real rather than a shrug. ``severity`` is
    Samsara's own read; a critical from the vehicle escalates a guess but never
    softens a known answer, because the table was written for these vehicles and
    Samsara's severity is generic.
    """
    key = (code or "").strip().upper()
    known = KNOWN.get(key)
    if known is not None:
        out = dict(known)
    else:
        guess = family(key)
        if guess is None:
            out = {"system": "", "plain": "", "consequence": "", "verdict": BOOK,
                   "note": "", "confident": False}
        else:
            out = dict(guess)
            if (severity or "").lower() == "critical" and out["verdict"] == BOOK:
                out["verdict"] = HOLD

    out["code"] = key
    out["raw"] = (description or "").strip()
    out["label"] = VERDICT_LABEL[out["verdict"]]
    out["tone"] = VERDICT_TONE[out["verdict"]]

    if not out["confident"]:
        # Say plainly that this is a guess from the code's shape. He can then
        # ring the shop with the code instead of trusting a sentence we made up.
        out["plain"] = out["plain"] or (
            f"Not one we have a plain-English entry for. From the code it is "
            f"{out['system'].lower()}." if out["system"]
            else "Not a code we recognise.")
        out["consequence"] = out["consequence"] or (
            "Treat the car's own wording below as the detail, and ring the shop "
            "with the code before a long run.")
    return out


def worst(explanations):
    """The verdict for a car carrying several codes: the most serious one."""
    if not explanations:
        return None
    return min(explanations, key=lambda e: VERDICT_RANK[e["verdict"]])


def summarise(explanations):
    """One sentence for a car with several codes at once.

    Deliberately not "4 faults". The founder's fleet throws DEF codes in packs
    of four, and four lines that are one problem is the alert-fatigue failure
    ``fleet_attention`` exists to avoid.
    """
    if not explanations:
        return ""
    systems = []
    for e in explanations:
        if e["system"] and e["system"] not in systems:
            systems.append(e["system"])
    top = worst(explanations)
    # The row title already names the system, so lead with the ANSWER — that is
    # the sentence he is reading the row for.
    lead = {
        HOLD: "Don't send it out.",
        BOOK: "It will run, but book the shop.",
        DRIVE: "Fine to run.",
    }[top["verdict"]]
    if len(systems) > 1:
        others = len(systems) - 1
        lead += (f" Also {systems[1].lower()}." if others == 1
                 else f" Plus {others} other systems.")
    return lead
