"""Draw what a flight refresh changed, onto the board's own geometry.

Pure presentation. It takes the change set worked out by ``ops.move_impact``
and turns it into percentages the timeline can position: where a leg used to
sit, the line from there to where it sits now, and the bracket over the part of
a turn that no longer works.

Writes nothing. Reads no legs. It is handed the rows the board has already
built and decorates them in place, so it costs one pass over the roster and
never a query.

Geometry, once, so the rest reads plainly: the timeline is a strip running from
``day_left_dt`` for ``total_display_minutes``, and every position is a
percentage of that strip. A slot already carries ``position_pct`` and
``width_pct`` on exactly that scale, so a ghost is the same width at the old
time, and a bracket is the span between two slot edges.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

#: A bracket narrower than this is invisible and unclickable.
MIN_BRACKET_PCT = 1.4
#: Ghosts closer than this to the real slot are noise, not information.
MIN_GHOST_SHIFT_PCT = 0.4

#: Brackets get their own band UNDER the pills, and the row grows to hold it.
#:
#: They cannot share the pill band. A lane is 30px of pill with a 2px gap
#: (views._DRIVER_LANE_H / _DRIVER_LANE_GAP), so a labelled bracket drawn at a
#: lane's bottom edge lands on top of the next chauffeur's jobs — which is
#: exactly what it did on the first cut: "76 min short" sitting across someone
#: else's 10 AM. Giving it its own strip is the only placement that cannot
#: collide, whatever the day looks like.
BRACKET_LANE_H = 15
#: Two brackets that overlap horizontally stack instead of printing over
#: each other.
BRACKET_LANE_GAP = 2
#: Horizontal breathing room required to call two brackets non-overlapping.
BRACKET_CLEARANCE_PCT = 0.6


def _pct(minutes_from_left, total_display_minutes):
    if not total_display_minutes:
        return 0.0
    return round(max(0.0, minutes_from_left / total_display_minutes * 100), 2)


def annotate(inhouse_timeline, changes, *, selected_date, day_left_dt,
             total_display_minutes):
    """Decorate rows and slots in place. Returns the ordered list of breaks.

    The returned list is what the banner's Prev/Next steps through, in the same
    order the rows are drawn, so "next" always means "further down the board".
    """
    if changes is None or changes.is_empty:
        return []

    ordered_breaks = []

    for row in inhouse_timeline:
        driver = row.get("driver")
        driver_id = getattr(driver, "id", None)
        mark = changes.row(driver_id) if driver_id is not None else None

        row["chg_breaks"] = mark["breaks"] if mark else 0
        row["chg_moved"] = bool(mark and mark["moved"])
        row["chg_marked"] = bool(mark and (mark["breaks"] or mark["moved"]))
        # The driver column has to be enough on its own to find the row.
        row["chg_badge"] = (
            f"{row['chg_breaks']}" if row["chg_breaks"]
            else ("moved" if row["chg_moved"] else "")
        )
        row["chg_badge_title"] = (
            f"{row['chg_breaks']} broken turn{'' if row['chg_breaks'] == 1 else 's'}"
            if row["chg_breaks"]
            else ("Pickup time moved on this row" if row["chg_moved"] else "")
        )
        row["chg_brackets"] = []

        slots = getattr(row.get("schedule"), "slots", None) or []
        by_leg = {}
        for slot in slots:
            leg_id = getattr(slot, "leg_id", None)
            by_leg[leg_id] = slot
            flags = changes.leg(leg_id) if leg_id is not None else None

            slot.chg_in_break = bool(flags and flags["in_break"])
            move = flags["move"] if flags else None
            slot.chg_moved = move is not None
            slot.chg_delta = move.minutes if move else 0
            # "−48" / "+22". A signed number is read faster than a sentence.
            slot.chg_delta_label = (
                f"{'+' if move.minutes > 0 else '−'}{abs(move.minutes)}"
                if move and move.minutes else ""
            )
            slot.chg_was_label = move.was_label if move else ""
            slot.chg_ghost = None
            slot.chg_link = None

            if move is None or move.was_time is None:
                continue

            # Where it used to sit: same pill, old time.
            was_dt = datetime.combine(
                move.was_date or selected_date, move.was_time,
            )
            ghost_left = _pct(
                (was_dt - day_left_dt).total_seconds() / 60.0,
                total_display_minutes,
            )
            now_left = float(getattr(slot, "position_pct", 0) or 0)
            width = float(getattr(slot, "width_pct", 0) or 0)
            if abs(ghost_left - now_left) < MIN_GHOST_SHIFT_PCT:
                continue

            slot.chg_ghost = {
                "left_pct": ghost_left,
                "width_pct": round(min(width, max(0.0, 100 - ghost_left)), 2),
            }
            # The line from there to here. Drawn between the facing edges so the
            # arrow never runs underneath either pill.
            if ghost_left < now_left:
                start = ghost_left + slot.chg_ghost["width_pct"]
                end, direction = now_left, "right"
            else:
                start, end, direction = now_left + width, ghost_left, "left"
            slot.chg_link = {
                "left_pct": round(min(start, end), 2),
                "width_pct": round(max(abs(end - start), 0.3), 2),
                "direction": direction,
            }

        # Brackets: one per broken turn on this row, over the part that fails.
        for clash in changes.breaks:
            if clash.driver_id != driver_id:
                continue
            prev_slot = by_leg.get(clash.prev_leg.pk)
            curr_slot = by_leg.get(clash.curr_leg.pk)
            if prev_slot is None or curr_slot is None:
                # The pair is not both drawn on this board (a filtered view, or
                # an overnight tail). Nothing to span; the row marker still says
                # something is wrong here.
                continue

            prev_end = (float(prev_slot.position_pct or 0)
                        + float(prev_slot.width_pct or 0))
            curr_start = float(curr_slot.position_pct or 0)
            if prev_end > curr_start:
                left, width = curr_start, prev_end - curr_start   # real overlap
            else:
                left, width = prev_end, curr_start - prev_end     # travel shortfall
            width = max(width, MIN_BRACKET_PCT)
            left = max(0.0, min(left, 100 - width))

            anchor = f"chg-{clash.driver_id}-{clash.prev_leg.pk}-{clash.curr_leg.pk}"
            bracket = {
                "anchor": anchor,
                "left_pct": round(left, 2),
                "width_pct": round(width, 2),
                "label": f"{clash.late_now} min short",
                "short_label": f"{clash.late_now}",
                "tier": clash.tier,
                "title": clash.why,
                "top_px": 0,          # set once the band is packed, below
            }
            row["chg_brackets"].append(bracket)
            ordered_breaks.append({
                "anchor": anchor,
                "driver_name": clash.driver_name,
                "label": bracket["label"],
                "why": clash.why,
                "tier": clash.tier,
            })

        _pack_brackets(row)

    return ordered_breaks


def _pack_brackets(row):
    """Lay the row's brackets out below the pills, and grow the row to fit.

    Greedy by left edge: a bracket joins the first band line it does not touch,
    otherwise it starts a new one. Then the row's bar is made tall enough to
    hold every line, so nothing spills onto the chauffeur underneath.
    """
    brackets = row.get("chg_brackets") or []
    if not brackets:
        return

    lanes = int(row.get("row_lanes") or 1)
    base = row.get("row_bar_height") or (lanes * 32 + 2)

    lines = []            # each is the right-most edge used so far on that line
    for bracket in sorted(brackets, key=lambda b: b["left_pct"]):
        placed = False
        for index, right_edge in enumerate(lines):
            if bracket["left_pct"] >= right_edge + BRACKET_CLEARANCE_PCT:
                bracket["top_px"] = base + index * (BRACKET_LANE_H + BRACKET_LANE_GAP)
                lines[index] = bracket["left_pct"] + bracket["width_pct"]
                placed = True
                break
        if not placed:
            bracket["top_px"] = base + len(lines) * (BRACKET_LANE_H + BRACKET_LANE_GAP)
            lines.append(bracket["left_pct"] + bracket["width_pct"])

    row["row_bar_height"] = (
        base + len(lines) * (BRACKET_LANE_H + BRACKET_LANE_GAP) + 2
    )
    row["chg_needs_height"] = True
