"""Transactional partner transitions shared by public, agency and staff tools."""
import hashlib
import secrets
import unicodedata
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from .models import (TravelAgent, Agency, PartnerIdentity, PartnerToken, AgencyAlias,
    AgencyApplication, AffiliationClaim, AgencyMembership, PartnerBooking,
    PartnerEvent, PartnerOutbox, PartnerBackfill, PartnerNameLock, PartnerPayoutAdjustment, CommissionPayout, AgencyCommissionPayout)

TERMS_VERSION = '2026-09-08'
DISCLOSURE_VERSION = '2026-09-07'
OPEN_CLAIMS = ['unmatched', 'ambiguous', 'candidate', 'payment_pending', 'rejected']


def normalize_name(value):
    return ' '.join(unicodedata.normalize('NFKC', value or '').casefold().split())


@transaction.atomic
def create_personal_user(username, email, password):
    """Serialize both public registration entry points without changing legacy IDs."""
    username, email = username.strip().lower(), email.strip().lower()
    keys = ['identity:' + hashlib.sha256(value.encode()).hexdigest()
            for value in ('username:' + username, 'email:' + email)]
    for key in sorted(keys):
        lock, _ = PartnerNameLock.objects.get_or_create(key=key)
        PartnerNameLock.objects.select_for_update().get(pk=lock.pk)
    if User.objects.filter(Q(username__iexact=username) | Q(email__iexact=email)).exists():
        raise ValidationError('This account already exists. Sign in to continue.')
    return User.objects.create_user(username=username, email=email, password=password)


def absolute_url(name, **kwargs):
    return getattr(settings, 'PARTNER_PUBLIC_ORIGIN', 'https://www.graysontowncar.com').rstrip('/') + reverse(name, kwargs=kwargs or None)


def event(actor, kind, obj, **details):
    return PartnerEvent.objects.create(actor=actor, kind=kind, object_type=obj.__class__.__name__, object_id=obj.pk, details=details)


def enqueue(key, recipient, subject, body):
    return PartnerOutbox.objects.get_or_create(key=key, defaults=dict(recipient=recipient, subject=subject, body=body))[0]


def notify(obj, kind, body):
    user = obj.agent.user if isinstance(obj, AffiliationClaim) else obj.applicant
    enqueue(f'{kind}:{obj.__class__.__name__}:{obj.pk}:{obj.state}', user.email,
        'Your Grayson partner account', body + '\n\n' + absolute_url('partner_setup'))


def require_staff(actor, reason=''):
    if not actor.is_active or not actor.is_staff:
        raise PermissionDenied
    if not reason.strip():
        raise ValidationError('Please record a reason for this staff decision.')


def managed_agencies(user):
    if not user.is_authenticated:
        return Agency.objects.none()
    return Agency.objects.filter(is_active=True, memberships__user=user,
        memberships__active=True, memberships__role__in=['owner', 'admin']).distinct()


def require_manager(actor, agency, owner=False):
    if actor.is_active and actor.is_staff:
        return
    roles = ['owner'] if owner else ['owner', 'admin']
    if not actor.is_active or not agency.is_active or not AgencyMembership.objects.filter(
        agency=agency, user=actor, active=True, role__in=roles).exists():
        raise PermissionDenied


def email_verified(user):
    identity = PartnerIdentity.objects.filter(user=user).first()
    return bool(identity and identity.email_verified)


@transaction.atomic
def issue_verification(user):
    User.objects.select_for_update().get(pk=user.pk)
    recent = PartnerToken.objects.filter(user=user, purpose='verify', used_at=None,
        expires_at__gt=timezone.now() + timedelta(hours=23, minutes=59)).exists()
    if recent:
        return
    raw = secrets.token_urlsafe(32)
    token = PartnerToken.objects.create(digest=hashlib.sha256(raw.encode()).hexdigest(),
        purpose='verify', user=user, email=user.email.strip(), expires_at=timezone.now()+timedelta(hours=24))
    enqueue(f'verify:{token.pk}', user.email, 'Verify your Grayson partner email',
        'You can already book. Verify your email to complete partner setup:\n' + absolute_url('partner_verify', token=raw))


@transaction.atomic
def verify_email(raw):
    token = PartnerToken.objects.select_for_update().filter(digest=hashlib.sha256(raw.encode()).hexdigest(), purpose='verify').first()
    if not token or token.expires_at < timezone.now():
        raise ValidationError('This verification link has expired. Request a new one from your account.')
    user = User.objects.select_for_update().get(pk=token.user_id)
    if token.email.casefold() != user.email.strip().casefold():
        raise ValidationError('Your email changed. Please request a new verification link.')
    if User.objects.filter(email__iexact=user.email.strip()).exclude(pk=user.pk).exists():
        raise ValidationError('This email is shared by multiple accounts. Grayson must reconcile it before verification.')
    if token.used_at:
        return user
    identity, _ = PartnerIdentity.objects.get_or_create(user=user, defaults={'legacy': True})
    identity.registration_email = user.email.strip().lower()
    identity.verified_email, identity.verified_at = user.email.strip(), timezone.now()
    identity.save()
    token.used_at = timezone.now()
    token.save(update_fields=['used_at'])
    for claim in AffiliationClaim.objects.filter(agent__user=user, state__in=['unmatched', 'ambiguous']):
        match_claim(claim)
    event(user, 'email_verified', identity)
    return user


@transaction.atomic
def inquiry_continuation(inquiry):
    raw = secrets.token_urlsafe(32)
    PartnerToken.objects.create(digest=hashlib.sha256(raw.encode()).hexdigest(), purpose='inquiry',
        inquiry=inquiry, email=inquiry.email, expires_at=timezone.now()+timedelta(days=7))
    url = absolute_url('partner_continue', token=raw)
    enqueue(f'inquiry:{inquiry.pk}', inquiry.email, 'Continue your Grayson partner registration',
        'Thank you for your interest in partnering with Grayson Towncar.\n'
        'Your partner account is ready to be created, and you can start booking immediately. '
        'No application call, upfront cost, or minimum bookings required.\n' + url)
    enqueue(f'inquiry-staff:{inquiry.pk}', 'admin@graysontowncar.com', 'Partner follow-up requested',
        f'{inquiry.name} requested contact. They can also register immediately.\n' + absolute_url('partner_staff'))
    return raw


@transaction.atomic
def submit_claim(agent, name, website, requested_payee, disclosure, actor):
    agent = TravelAgent.objects.select_for_update().get(pk=agent.pk)
    if not disclosure or requested_payee not in ('direct', 'agency') or not name.strip():
        raise ValidationError('Confirm your affiliation disclosure and payment preference.')
    previous = AffiliationClaim.objects.filter(agent=agent, state__in=OPEN_CLAIMS).order_by('-pk').first()
    if previous and normalize_name(previous.name) == normalize_name(name) and previous.requested_payee == requested_payee and previous.state != 'rejected':
        return previous
    # Outstanding commissions stay tied to their original request; correction must not erase holds.
    AffiliationClaim.objects.filter(agent=agent, state__in=OPEN_CLAIMS).update(state='superseded')
    claim = AffiliationClaim.objects.create(agent=agent, name=name.strip(), website=website,
        requested_payee=requested_payee, disclosure_version=DISCLOSURE_VERSION, disclosed_at=timezone.now())
    agent.agency_name = name.strip()
    agent.save(update_fields=['agency_name'])
    event(actor, 'affiliation_submitted', claim)
    return match_claim(claim)


@transaction.atomic
def match_claim(claim):
    claim = AffiliationClaim.objects.select_for_update().select_related('agent__user').get(pk=claim.pk)
    if claim.state not in ['unmatched', 'ambiguous', 'candidate']:
        return claim
    if not claim.disclosed_at or not claim.disclosure_version or not email_verified(claim.agent.user):
        return claim
    key = normalize_name(claim.name)
    # Deliberately no email-domain or fuzzy matching.
    ids = {a.pk for a in Agency.objects.filter(is_active=True) if normalize_name(a.name) == key}
    ids.update(AgencyAlias.objects.filter(normalized_name=key, agency__is_active=True).values_list('agency_id', flat=True))
    claim.agency_id = next(iter(ids)) if len(ids) == 1 else None
    claim.state = 'candidate' if len(ids) == 1 else ('ambiguous' if ids else 'unmatched')
    claim.save(update_fields=['agency', 'state'])
    if claim.state == 'candidate':
        for member in claim.agency.memberships.filter(active=True, role__in=['owner','admin']).select_related('user'):
            enqueue(f'candidate:{claim.pk}:{claim.agency_id}:{member.user_id}', member.user.email,
                'An agent is ready for your review', 'Review the person who identified your agency:\n' + absolute_url('partner_agency', pk=claim.agency_id))
    return claim


def rematch_claims():
    for claim in AffiliationClaim.objects.filter(state__in=['unmatched','ambiguous','candidate']).iterator():
        match_claim(claim)


@transaction.atomic
def assign_member(actor, agent, agency, payee, reason='', role='agent'):
    if not agency.is_active:
        raise ValidationError('This agency is inactive. Grayson must resolve its status before adding members.')
    if payee not in ['direct','agency','pending'] or role not in ['agent','admin','owner']:
        raise ValidationError('Invalid membership or payment arrangement.')
    if actor.is_staff:
        require_staff(actor, reason)
    else:
        require_manager(actor, agency)
        if role != 'agent':
            raise PermissionDenied
    # Serializes membership transitions and booking creation for this person.
    agent = TravelAgent.objects.select_for_update().select_related('user').get(pk=agent.pk)
    previous = list(AgencyMembership.objects.select_for_update().filter(user=agent.user, active=True, booking_affiliation=True).exclude(agency=agency))
    for old in previous:
        if old.role in ['owner','admin']:
            old.booking_affiliation = False
        else:
            old.active, old.ended_at = False, timezone.now()
        old.save()
    member = AgencyMembership.objects.filter(user=agent.user, agency=agency, active=True).first()
    if not member:
        member = AgencyMembership(agency=agency, user=agent.user, role=role)
    elif role == 'owner':
        member.role = role
    member.booking_affiliation, member.payee = True, payee
    member.save()
    agent.agency = agency
    agent.agency_handles_payment = payee == 'agency'
    agent.save(update_fields=['agency', 'agency_handles_payment'])
    if member.role in ['owner','admin']:
        agency.heads.add(agent.user)
    if actor.is_staff and payee != 'pending' and role != 'owner':
        # Staff assignment controls future affiliation. Historical held bookings
        # remain untouched until a separate reviewed decision/backfill.
        AffiliationClaim.objects.filter(agent=agent, state__in=OPEN_CLAIMS).update(
            state='approved', agency=agency, offered_payee=payee, decided_at=timezone.now(), decision_reason=reason)
    event(actor, 'member_assigned', member, reason=reason, payee=payee)
    return member


@transaction.atomic
def review_candidate(actor, claim_id, action, payee='', reason=''):
    agent_id = AffiliationClaim.objects.values_list('agent_id', flat=True).get(pk=claim_id)
    TravelAgent.objects.select_for_update().get(pk=agent_id)
    claim = AffiliationClaim.objects.select_for_update(of=('self',)).select_related('agency','agent__user').get(pk=claim_id)
    if not claim.agency_id:
        raise ValidationError('Grayson must first resolve the agency match.')
    require_manager(actor, claim.agency)
    if actor.is_staff:
        require_staff(actor, reason)
    if claim.state in ['approved','rejected']:
        return claim
    if claim.state != 'candidate' or not email_verified(claim.agent.user) or not claim.disclosed_at:
        raise ValidationError('This candidate is not ready for review.')
    if action == 'reject':
        claim.state, claim.decision_reason = 'rejected', reason or 'The agency could not confirm this affiliation.'
    elif action == 'add':
        if payee not in ['agency','direct']:
            raise ValidationError('Choose the approved payment arrangement.')
        # An agency that pays its own agents cannot approve a direct arrangement,
        # however the agent filled in the form.
        if payee == 'direct' and claim.agency.payout_policy == 'agency':
            raise ValidationError('This agency is always the payee. Change the agency payment policy first if this agent should be paid directly.')
        claim.offered_payee = payee
        agreed = payee == claim.requested_payee
        assign_member(actor, claim.agent, claim.agency, payee if agreed else 'pending', reason)
        claim.state = 'approved' if agreed else 'payment_pending'
        if agreed:
            resolve_claim_bookings(claim, payee)
    else:
        raise ValidationError('Unknown review action.')
    claim.decided_at = timezone.now()
    claim.save()
    event(actor, 'candidate_' + action, claim, payee=payee, reason=reason)
    notice = ('The agency could not confirm your affiliation. Please correct your agency name or request independent status from your account. Existing disputed commissions remain held for Grayson review.'
              if action == 'reject' else 'Your agency review has been updated. Check your membership and payment status.')
    notify(claim, 'candidate-decision', notice)
    return claim


def resolve_claim_bookings(claim, payee):
    # Resolves money only. Agency visibility never changes retrospectively here.
    PartnerBooking.objects.filter(claim=claim, reservation__commission_paid=False).update(
        payee_agency=claim.agency if payee == 'agency' else None, hold='')


@transaction.atomic
def accept_payment(actor, claim_id):
    agent_id = AffiliationClaim.objects.values_list('agent_id', flat=True).get(pk=claim_id)
    TravelAgent.objects.select_for_update().get(pk=agent_id)
    claim = AffiliationClaim.objects.select_for_update(of=('self',)).select_related('agent','agency').get(pk=claim_id, agent__user=actor)
    if claim.state == 'approved':
        return
    if claim.state != 'payment_pending':
        raise ValidationError('No payment proposal is awaiting your acceptance.')
    member = AgencyMembership.objects.select_for_update().filter(user=actor, agency=claim.agency, active=True, booking_affiliation=True).first()
    if not member or not claim.agency.is_active:
        raise ValidationError('This membership is no longer active. Contact Grayson.')
    member.payee = claim.offered_payee
    member.save(update_fields=['payee'])
    TravelAgent.objects.filter(pk=claim.agent_id).update(agency_handles_payment=member.payee == 'agency')
    claim.state, claim.decided_at = 'approved', timezone.now()
    claim.save()
    resolve_claim_bookings(claim, member.payee)
    event(actor, 'payment_accepted', claim, payee=member.payee)
    notify(claim, 'payment-accepted', 'Your agency payment arrangement is confirmed.')


@transaction.atomic
def remove_member(actor, membership_id, reason):
    user_id = AgencyMembership.objects.values_list('user_id',flat=True).get(pk=membership_id)
    list(TravelAgent.objects.select_for_update().filter(user_id=user_id))
    member = AgencyMembership.objects.select_for_update().select_related('agency','user').get(pk=membership_id)
    require_manager(actor, member.agency, owner=member.role != 'agent')
    if not reason.strip():
        raise ValidationError('Please provide a reason.')
    if not member.active:
        return
    # Agency lock protects the last-owner invariant across different member rows.
    Agency.objects.select_for_update().get(pk=member.agency_id)
    if member.role == 'owner' and not member.agency.memberships.filter(role='owner', active=True).exclude(pk=member.pk).exists():
        raise ValidationError('Appoint another Owner before removing the last Owner.')
    if member.booking_affiliation:
        TravelAgent.objects.filter(user=member.user, agency=member.agency).update(agency=None, agency_handles_payment=False)
    member.active, member.booking_affiliation, member.ended_at = False, False, timezone.now()
    member.save()
    member.agency.heads.remove(member.user)
    AffiliationClaim.objects.filter(agent__user=member.user, agency=member.agency, state__in=['candidate','payment_pending']).update(state='superseded')
    event(actor, 'member_removed', member, reason=reason)
    enqueue(f'member-removed:{member.pk}', member.user.email, 'Agency membership updated',
        'Your agency membership ended. You can still book through your personal Grayson account.\n' + absolute_url('partner_setup'))


@transaction.atomic
def change_role(actor, membership_id, role, reason):
    member = AgencyMembership.objects.select_for_update().select_related('agency').get(pk=membership_id, active=True)
    require_manager(actor, member.agency, owner=True)
    if role not in ['agent','admin','owner'] or not reason.strip():
        raise ValidationError('Choose a valid role and record a reason.')
    Agency.objects.select_for_update().get(pk=member.agency_id)
    if member.role == 'owner' and role != 'owner' and not member.agency.memberships.filter(active=True, role='owner').exclude(pk=member.pk).exists():
        raise ValidationError('The agency must retain an Owner.')
    member.role = role
    member.save(update_fields=['role'])
    if role == 'agent':
        member.agency.heads.remove(member.user)
    else:
        member.agency.heads.add(member.user)
    event(actor, 'role_changed', member, role=role, reason=reason)


@transaction.atomic
def review_application(actor, application_id, action, reason, agency_id=None):
    require_staff(actor, reason)
    app = AgencyApplication.objects.select_for_update().select_related('applicant').get(pk=application_id)
    if app.state == 'approved':
        return app.agency
    if action in ['rejected','information','pending']:
        app.state, app.decision_reason = action, reason
        app.save()
        event(actor, 'application_' + action, app, reason=reason)
        notify(app, 'application', reason)
        return
    if action != 'approved' or not email_verified(app.applicant):
        raise ValidationError('The applicant must verify their email before approval.')
    # Lock all existing application rows with this exact business name on PostgreSQL.
    # Canonical matching is deliberately advisory: staff may distinguish namesakes.
    key = normalize_name(app.name)
    lock, _ = PartnerNameLock.objects.get_or_create(key=key)
    PartnerNameLock.objects.select_for_update().get(pk=lock.pk)
    if not agency_id and any(normalize_name(a.name) == key for a in Agency.objects.all()):
        raise ValidationError('An agency with this name exists. Review it and select the existing record, or create a distinguished namesake through Grayson administration.')
    agency = Agency.objects.select_for_update().get(pk=agency_id) if agency_id else Agency.objects.create(
        name=app.name, website=app.website, phone=app.phone, address=app.address, is_active=True)
    if not agency.is_active:
        raise ValidationError('Reactivate the agency before assigning ownership.')
    agent = TravelAgent.objects.get(user=app.applicant)
    assign_member(actor, agent, agency, 'agency', reason, role='owner')
    app.state, app.agency, app.decision_reason, app.decided_at = 'approved', agency, reason, timezone.now()
    app.save()
    # Application signup has an explicitly disclosed agency-paid affiliation.
    for claim in AffiliationClaim.objects.filter(agent=agent, state__in=OPEN_CLAIMS):
        if normalize_name(claim.name) == normalize_name(app.name):
            claim.agency, claim.state, claim.offered_payee, claim.decided_at = agency, 'approved', 'agency', timezone.now()
            claim.save()
            resolve_claim_bookings(claim, 'agency')
    event(actor, 'agency_approved', app, agency_id=agency.pk, reason=reason)
    notify(app, 'application', 'Your agency is approved. Your private agency workspace is ready.')
    rematch_claims()
    return agency


@transaction.atomic
def capture_booking(reservation):
    if not reservation.travel_agent_id:
        return
    if PartnerBooking.objects.filter(reservation=reservation).exists():
        return
    agent = TravelAgent.objects.select_for_update().get(pk=reservation.travel_agent_id)
    member = AgencyMembership.objects.filter(user=agent.user, active=True, booking_affiliation=True).select_related('agency').first()
    claim = AffiliationClaim.objects.filter(agent=agent, state__in=OPEN_CLAIMS).order_by('-pk').first()
    identity = PartnerIdentity.objects.filter(user=agent.user).first()
    legacy = identity is None or identity.legacy
    agency = member.agency if member else (agent.agency if legacy else None)
    payee_agency = agency if (member and member.payee == 'agency') or (not member and legacy and agent.agency_handles_payment) else None
    hold = 'Agency / payment confirmation pending' if claim or (member and member.payee == 'pending') else ''
    if legacy and not member and ((agent.agency_handles_payment and not agent.agency_id) or agent.payment_method == 'agency'):
        hold = 'Legacy payment routing requires review'
    PartnerBooking.objects.get_or_create(reservation=reservation, defaults=dict(agent=agent,
        agency=agency, payee_agency=payee_agency, claim=claim, hold=hold, legacy=legacy))
    if identity and not identity.first_booking_at:
        PartnerIdentity.objects.filter(pk=identity.pk, first_booking_at=None).update(first_booking_at=timezone.now())


def payout_blocker(reservation):
    try:
        context = reservation.partner_context
    except PartnerBooking.DoesNotExist:
        return 'Booking attribution requires staff review'
    if context.agent_id != reservation.travel_agent_id:
        return 'Agent attribution changed; staff review required'
    if context.hold:
        return context.hold
    if context.agency_id and not context.agency.is_active:
        return 'Agency is suspended'
    if context.payee_agency_id and not context.payee_agency.is_active:
        return 'Payee agency is suspended'
    identity = PartnerIdentity.objects.filter(user_id=context.agent.user_id).select_related('user').first()
    if identity and not identity.legacy:
        if not identity.email_verified:
            return 'Email verification pending'
        if not identity.terms_at:
            return 'Partner terms acceptance pending'
        payee = context.payee_agency or context.agent
        if not payee.payment_method or payee.payment_method == 'agency' or not (payee.payment_info or '').strip():
            return 'Payment details incomplete'
    return ''


def payee_reservations(agent=None, agency=None):
    from reservations.models import Reservation
    qs = Reservation.objects.filter(partner_context__payee_agency=agency)
    if agent:
        qs = qs.filter(travel_agent=agent)
    return qs.select_related('partner_context__agency','partner_context__payee_agency','partner_context__agent__user',
        'travel_agent__user','customer','rate__route__origin','rate__route__destination').prefetch_related('legs')


@transaction.atomic
def process_recorded_payout(agent, agency=None, create_agency_payout=True):
    from users.eligibility import get_commission_eligibility
    # Lock agent first, then reservations, in every entry point.
    agent = TravelAgent.objects.select_for_update().get(pk=agent.pk)
    rows = payee_reservations(agent, agency).filter(commission_paid=False).order_by('pk')
    ids = list(rows.values_list('pk', flat=True))
    from reservations.models import Reservation
    list(Reservation.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
    ready = [(r, get_commission_eligibility(r)) for r in rows]
    ready = [(r,e) for r,e in ready if e.safe_to_pay]
    if not ready:
        return None, Decimal('0'), None
    total = sum((e.commission for r,e in ready), Decimal('0'))
    dates = [leg.pickup_date for r,e in ready for leg in r.legs.all() if leg.pickup_date]
    start, end = min(dates) if dates else timezone.localdate(), timezone.localdate()
    payout = CommissionPayout.objects.create(agent=agent, agency=agency, total_amount=total,
        payout_period_start=start, payout_period_end=end, notes='Recorded booking payee; membership changes do not reroute this payout.')
    payout.reservations.set([r.pk for r,e in ready])
    for res, result in ready:
        Reservation.objects.filter(pk=res.pk, commission_paid=False).update(commission_paid=True,
            commission_paid_at=timezone.now(), commission_amount=result.commission)
    agency_payout = None
    if agency and create_agency_payout:
        agency_payout = AgencyCommissionPayout.objects.create(agency=agency, total_amount=total,
            payout_period_start=start, payout_period_end=end)
        agency_payout.agent_payouts.add(payout)
    agent.last_payment_date = timezone.now()
    agent.save(update_fields=['last_payment_date'])
    agent.sync_paid_commission()
    agent.update_unpaid_commissions()
    return payout, total, agency_payout


@transaction.atomic
def process_recorded_agency_payout(agency):
    agency = Agency.objects.select_for_update().get(pk=agency.pk)
    if not agency.is_active:
        raise ValidationError('Agency is suspended.')
    ids = payee_reservations(agency=agency).filter(commission_paid=False).values_list('travel_agent_id',flat=True).distinct()
    children = []
    for agent in TravelAgent.objects.filter(pk__in=ids).order_by('pk'):
        child, amount, _ = process_recorded_payout(agent, agency, False)
        if child:
            children.append(child)
    if not children:
        return None, Decimal('0')
    total = sum((p.total_amount for p in children), Decimal('0'))
    payout = AgencyCommissionPayout.objects.create(agency=agency,total_amount=total,
        payout_period_start=min(p.payout_period_start for p in children), payout_period_end=timezone.localdate())
    payout.agent_payouts.set(children)
    agency.sync_paid_commission()
    return payout, total


def backfill_row(context):
    from users.eligibility import _commission_for
    r = context.reservation
    dates = sorted(str(leg.pickup_date) for leg in r.legs.all() if leg.pickup_date)
    return dict(reservation=r.pk, agent=context.agent_id, reservation_agent=r.travel_agent_id,
        agency=context.agency_id, payee=context.payee_agency_id, claim=context.claim_id,
        hold=context.hold, paid=r.commission_paid, amount=str(r.commission_amount or 0),
        commission=str(r.commission_amount or 0) if r.commission_paid else str(_commission_for(r)),
        base=str(r.base_price), rate=str(r.travel_agent.commission_rate) if r.travel_agent_id else None,
        date_from=dates[0] if dates else '', date_to=dates[-1] if dates else '',
        status=r.status, excluded=r.commission_excluded, refunded=str(r.total_refunded))


@transaction.atomic
def preview_backfill(actor, reservation_ids, agency, visibility, payment, payee, reason):
    require_staff(actor, reason)
    if not visibility and not payment:
        raise ValidationError('Choose visibility, unpaid payment routing, or both.')
    if payee not in ['direct','agency'] or (payment and payee == 'agency' and not agency):
        raise ValidationError('Select a valid payee.')
    ids = set(reservation_ids)
    contexts = list(PartnerBooking.objects.filter(reservation_id__in=ids).select_related('reservation__travel_agent').prefetch_related('reservation__legs'))
    if not ids or len(ids) > 500 or len(contexts) != len(ids):
        raise ValidationError('Select between 1 and 500 bookings with recorded partner attribution.')
    rows = []
    for c in contexts:
        r = c.reservation
        if payment and r.commission_paid:
            raise ValidationError(f'Booking {r.pk} has a completed payout; use an adjustment instead.')
        rows.append(backfill_row(c))
    return PartnerBackfill.objects.create(actor=actor, agency=agency, rows=rows, change_visibility=visibility,
        change_payee=payment,payee=payee,reason=reason)


@transaction.atomic
def apply_backfill(actor, preview_id):
    preview = PartnerBackfill.objects.select_for_update().get(pk=preview_id, actor=actor)
    require_staff(actor, preview.reason)
    if preview.applied_at:
        return preview
    if preview.created_at < timezone.now()-timedelta(minutes=30):
        raise ValidationError('Preview expired. Generate a fresh preview.')
    from reservations.models import Reservation
    # Use the same agent-before-booking lock order as payout processing.
    list(TravelAgent.objects.select_for_update().filter(pk__in=[r['agent'] for r in preview.rows]).order_by('pk'))
    list(Reservation.objects.select_for_update().filter(pk__in=[r['reservation'] for r in preview.rows]).order_by('pk'))
    for row in preview.rows:
        c = PartnerBooking.objects.select_for_update().select_related('reservation').get(reservation_id=row['reservation'])
        current = backfill_row(c)
        if current != row:
            raise ValidationError('A booking changed after preview. Generate a fresh preview.')
        if preview.change_visibility:
            c.agency = preview.agency
        if preview.change_payee:
            c.payee_agency = preview.agency if preview.payee == 'agency' else None
            c.hold, c.claim = '', None
        c.save()
        event(actor,'booking_backfilled',c,before=row,agency=c.agency_id,payee=c.payee_agency_id,reason=preview.reason)
    preview.applied_at = timezone.now()
    preview.save(update_fields=['applied_at'])
    return preview


@transaction.atomic
def confirm_independent(actor, agent, reason):
    require_staff(actor, reason)
    agent = TravelAgent.objects.select_for_update().get(pk=agent.pk)
    for member in AgencyMembership.objects.filter(user=agent.user,active=True,booking_affiliation=True):
        member.booking_affiliation = False
        if member.role == 'agent':
            member.active, member.ended_at = False, timezone.now()
        member.save()
    agent.agency, agent.agency_handles_payment, agent.agency_name = None, False, ''
    agent.save(update_fields=['agency','agency_handles_payment','agency_name'])
    AffiliationClaim.objects.filter(agent=agent,state__in=OPEN_CLAIMS).update(state='superseded',decided_at=timezone.now(),decision_reason=reason)
    event(actor,'independence_confirmed',agent,reason=reason)
    enqueue(f'independent:{agent.pk}:{timezone.now().isoformat()}',agent.user.email,'Your affiliation is updated',
        'Your future bookings are independent. Any historical payout corrections are reviewed separately.\n'+absolute_url('partner_setup'))


@transaction.atomic
def record_payout_adjustment(actor, payout_id, amount, reference, reason):
    require_staff(actor, reason)
    payout = CommissionPayout.objects.select_for_update().get(pk=payout_id)
    reference = reference.strip()
    if not reference or not amount.is_finite() or amount == 0 or amount != amount.quantize(Decimal('0.01')):
        raise ValidationError('Provide a nonzero amount with two decimal places and the payment reference.')
    existing = PartnerPayoutAdjustment.objects.filter(payout=payout,reference=reference).first()
    if existing:
        if existing.amount != amount or existing.reason != reason:
            raise ValidationError('That reference already records a different correction for this payout.')
        return existing
    row = PartnerPayoutAdjustment(payout=payout,amount=amount,reference=reference,reason=reason,actor=actor)
    row.full_clean();row.save()
    event(actor,'payout_adjusted',row,payout=payout.pk,amount=str(amount),reference=reference,reason=reason)
    return row
