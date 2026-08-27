"""One place that decides how a reservation's payment state reads on the board.

Three states, because a dispatcher does something different about each:

  paid        nothing to do
  card_saved  we hold a card — one click to collect in the payment portal
  unpaid      nobody has given us a card; someone has to ring the guest

Those middle two used to render identically, which is what this exists to fix:
a trip with a card on file wore the same amber "unpaid" ring and dollar sign as
one nobody could collect on, so the board could not tell a dispatcher which of
the two jobs was in front of them.

This does NOT re-derive the precedence. Reservation.payment_status
(reservations/models.py) is the single source of truth for reconciling several
Payment rows on one reservation, and its ordering — paid beating a leftover
booking-time card_saved row — is itself a shipped bug fix guarded by tests. Nine
call sites hand-writing that comparison is nine chances to resurrect it, so this
delegates and never compares payment rows itself.
"""

PAY_PAID = "paid"
PAY_CARD_SAVED = "card_saved"
PAY_UNPAID = "unpaid"


def board_pay_state(reservation):
    """Return 'paid' | 'card_saved' | 'unpaid' for a board slot.

    `reservation` may be None (an orphan leg) → 'paid', matching the
    long-standing `if leg.reservation else True` default at every call site that
    used to compute this inline.

    Everything that is neither paid nor card_saved collapses to 'unpaid', which
    keeps today's rendering for the pending/failed tail. Note payment_status has
    no 'refunded' branch and falls through to 'failed' — those legs genuinely
    still owe money, so they correctly keep the amber flag.
    """
    if reservation is None:
        return PAY_PAID
    status = reservation.payment_status   # precedence lives ONLY there
    if status == PAY_PAID:
        return PAY_PAID
    if status == PAY_CARD_SAVED:
        return PAY_CARD_SAVED
    return PAY_UNPAID
