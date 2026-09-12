"""Private onboarding state. Claimed affiliation never grants membership."""
import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class PartnerIdentity(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='partner_identity')
    registration_email = models.EmailField(null=True, blank=True, unique=True)
    verified_email = models.EmailField(blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    terms_version = models.CharField(max_length=30, blank=True)
    terms_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    first_booking_at = models.DateTimeField(null=True, blank=True)
    inquiry = models.ForeignKey('users.PartnerForm', null=True, blank=True, on_delete=models.SET_NULL)
    # Legacy accounts retain payout access; email attribution still needs verification.
    legacy = models.BooleanField(default=False)

    @property
    def email_verified(self):
        return bool(self.verified_at and self.verified_email.casefold() == self.user.email.strip().casefold())


class PartnerToken(models.Model):
    digest = models.CharField(max_length=64, unique=True)
    purpose = models.CharField(max_length=20)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.CASCADE)
    inquiry = models.ForeignKey('users.PartnerForm', null=True, on_delete=models.CASCADE)
    email = models.EmailField()
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True)


class AgencyAlias(models.Model):
    agency = models.ForeignKey('users.Agency', on_delete=models.CASCADE, related_name='aliases')
    name = models.CharField(max_length=200)
    normalized_name = models.CharField(max_length=200, db_index=True, editable=False)

    def save(self, *args, **kwargs):
        from .partner_services import normalize_name
        self.normalized_name = normalize_name(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class AgencyApplication(models.Model):
    applicant = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    name = models.CharField(max_length=100)
    website = models.URLField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    authority = models.TextField()
    state = models.CharField(max_length=20, default='pending', db_index=True,
        choices=[(x, x.title()) for x in ('pending', 'information', 'approved', 'rejected')])
    agency = models.ForeignKey('users.Agency', null=True, blank=True, on_delete=models.PROTECT)
    decision_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['applicant'], condition=Q(state__in=['pending', 'information']), name='one_open_agency_application')]


class AffiliationClaim(models.Model):
    agent = models.ForeignKey('users.TravelAgent', on_delete=models.CASCADE, related_name='affiliation_claims')
    name = models.CharField(max_length=100)
    website = models.URLField(blank=True)
    disclosure_version = models.CharField(max_length=30, blank=True)
    disclosed_at = models.DateTimeField(null=True, blank=True)
    requested_payee = models.CharField(max_length=10, choices=[('direct', 'Grayson pays me directly'), ('agency', 'Grayson pays my agency, and my agency pays me')])
    offered_payee = models.CharField(max_length=10, blank=True)
    agency = models.ForeignKey('users.Agency', null=True, blank=True, on_delete=models.PROTECT, related_name='claims')
    state = models.CharField(max_length=20, default='unmatched', db_index=True,
        choices=[(x, x.title()) for x in ('unmatched', 'ambiguous', 'candidate', 'payment_pending', 'approved', 'rejected', 'superseded')])
    created_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True)
    escalated_at = models.DateTimeField(null=True, blank=True)


class AgencyMembership(models.Model):
    agency = models.ForeignKey('users.Agency', on_delete=models.PROTECT, related_name='memberships')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='agency_memberships')
    role = models.CharField(max_length=10, choices=[('owner', 'Owner'), ('admin', 'Admin'), ('agent', 'Agent')], default='agent')
    active = models.BooleanField(default=True)
    booking_affiliation = models.BooleanField(default=False)
    payee = models.CharField(max_length=10, choices=[('direct', 'Direct'), ('agency', 'Agency'), ('pending', 'Pending')], default='pending')
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['agency', 'user'], condition=Q(active=True), name='one_active_agency_membership'),
            models.UniqueConstraint(fields=['user'], condition=Q(active=True, booking_affiliation=True), name='one_booking_affiliation'),
        ]


class PartnerBooking(models.Model):
    reservation = models.OneToOneField('reservations.Reservation', on_delete=models.CASCADE, related_name='partner_context')
    agent = models.ForeignKey('users.TravelAgent', on_delete=models.PROTECT, related_name='booking_contexts')
    agency = models.ForeignKey('users.Agency', null=True, blank=True, on_delete=models.PROTECT, related_name='booking_contexts')
    payee_agency = models.ForeignKey('users.Agency', null=True, blank=True, on_delete=models.PROTECT, related_name='payable_contexts')
    claim = models.ForeignKey(AffiliationClaim, null=True, blank=True, on_delete=models.PROTECT, related_name='booking_contexts')
    hold = models.CharField(max_length=100, blank=True)
    legacy = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)


class PartnerEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    kind = models.CharField(max_length=60)
    object_type = models.CharField(max_length=50)
    object_id = models.PositiveBigIntegerField()
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)


class PartnerOutbox(models.Model):
    key = models.CharField(max_length=180, unique=True)
    recipient = models.EmailField()
    subject = models.CharField(max_length=200)
    body = models.TextField()
    state = models.CharField(max_length=12, default='pending', db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    lease_until = models.DateTimeField(null=True)
    lease_id = models.UUIDField(null=True)
    last_error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(default=timezone.now)


class PartnerBackfill(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    agency = models.ForeignKey('users.Agency', null=True, on_delete=models.PROTECT)
    rows = models.JSONField(default=list)
    change_visibility = models.BooleanField(default=True)
    change_payee = models.BooleanField(default=False)
    payee = models.CharField(max_length=10, default='direct')
    reason = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    applied_at = models.DateTimeField(null=True)


class PartnerNameLock(models.Model):
    key = models.CharField(max_length=200, unique=True)


class PartnerPayoutAdjustment(models.Model):
    """Separate manual accounting correction; the completed payout stays immutable."""
    payout = models.ForeignKey('users.CommissionPayout', on_delete=models.PROTECT, related_name='partner_adjustments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reference = models.CharField(max_length=100)
    reason = models.TextField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['payout','reference'], name='unique_partner_adjustment_reference')]
