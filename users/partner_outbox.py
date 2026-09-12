"""Durable, leased delivery. SMTP may redeliver after an uncertain send; links are idempotent."""
import re
import uuid
from datetime import timedelta
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone
from .models import PartnerOutbox, PartnerIdentity, AffiliationClaim
from .partner_services import enqueue, absolute_url

# Framing per message type. The queued body stays the plain-text fallback; this
# only supplies the branding around it. Staff notices skip the program pitch.
_FRAMING = {
    'inquiry':        ('', 'Your clients are in good hands.', 'Create my partner account', True),
    'welcome':        ('', 'Your clients are in good hands.', 'Complete my setup', True),
    'verify':         ('Confirm your email', 'One quick step.', 'Verify my email', True),
    'setup-reminder': ('Partner setup', 'Nearly there.', 'Finish my setup', True),
    'candidate':      ('Agency review', 'Someone named your agency.', 'Review this person', False),
    'member-removed': ('Membership updated', 'Your affiliation has changed.', 'View my account', False),
    'independent':    ('Affiliation updated', 'Your affiliation has changed.', 'View my account', False),
    'inquiry-staff':  ('Partner administration', 'A partner asked for contact.', 'Open partner administration', False),
    'application':    ('Partner administration', 'An agency is waiting for review.', 'Open partner administration', False),
    'escalate':       ('Partner administration', 'A decision has been waiting seven days.', 'Open partner administration', False),
}
_DEFAULT_FRAMING = ('Your partner account', 'An update on your account.', 'View my account', False)
_URL = re.compile(r'https?://\S+')


def _branded_html(row):
    """Wrap a queued plain-text message in the partner email shell."""
    eyebrow, headline, action_label, show_program = _FRAMING.get(
        row.key.split(':')[0], _DEFAULT_FRAMING)
    lines = [ln.strip() for ln in (row.body or '').splitlines() if ln.strip()]
    # The queued body ends with the link; it becomes the button, not body copy.
    urls = _URL.findall(' '.join(lines))
    action_url = urls[-1] if urls else ''
    paragraphs = [ln for ln in lines if not _URL.fullmatch(ln)]
    return render_to_string('users/partners/email.html', {
        'subject': row.subject,
        'preheader': paragraphs[0] if paragraphs else row.subject,
        'eyebrow': eyebrow,
        'headline': headline,
        'paragraphs': paragraphs,
        'action_url': action_url,
        'action_label': action_label,
        'show_program': show_program,
        'assets': getattr(settings, 'PARTNER_EMAIL_ASSET_ORIGIN', 'https://www.graysontowncar.com').rstrip('/'),
        'partner_email': getattr(settings, 'PARTNER_CONTACT_EMAIL', 'reservations@graysontowncar.com'),
        'footer_note': 'You are receiving this because you started a partner account with Grayson Towncar.',
    })


def deliver_one():
    now=timezone.now()
    with transaction.atomic():
        eligible=PartnerOutbox.objects.filter(Q(state='pending',available_at__lte=now)|Q(state='sending',lease_until__lt=now))
        # Short transaction with DB lock; network send happens after releasing it.
        row=eligible.select_for_update().order_by('pk').first()
        if not row: return False
        row.state='sending';row.lease_until=now+timedelta(minutes=5);row.lease_id=uuid.uuid4();row.attempts+=1
        row.save()
    if row.key.startswith('setup-reminder:'):
        identity = PartnerIdentity.objects.select_related('user').filter(pk=row.key.split(':')[1]).first()
        agent = getattr(identity.user, 'travelagent', None) if identity else None
        unresolved = AffiliationClaim.objects.filter(agent=agent,state__in=['unmatched','ambiguous','candidate','payment_pending','rejected']).exists() if agent else False
        if not identity or (identity.email_verified and identity.terms_at and agent and agent.payment_info_complete and not unresolved):
            PartnerOutbox.objects.filter(pk=row.pk,lease_id=row.lease_id).update(state='cancelled',lease_until=None)
            return True
    # Testing runs against a copy of production, where every address is a real
    # agent or agency. An allowlist holds anything else before it can be sent.
    allowlist=getattr(settings,'PARTNER_EMAIL_ALLOWLIST',[])
    if allowlist and (row.recipient or '').strip().lower() not in allowlist:
        PartnerOutbox.objects.filter(pk=row.pk,lease_id=row.lease_id).update(
            state='held',lease_until=None,
            last_error='held: recipient is not on PARTNER_EMAIL_ALLOWLIST')
        return True
    try:
        message=EmailMultiAlternatives(row.subject,row.body,getattr(settings,'DEFAULT_FROM_EMAIL','reservations@graysontowncar.com'),[row.recipient],
            headers={'Message-ID':f'<partner-{row.pk}@graysontowncar.com>'})
        # Plain text stays the fallback body; the branded shell rides alongside it.
        message.attach_alternative(_branded_html(row),'text/html')
        message.send(fail_silently=False)
    except Exception as exc:
        PartnerOutbox.objects.filter(pk=row.pk,lease_id=row.lease_id,state='sending').update(
            state='failed' if row.attempts>=5 else 'pending',available_at=now+timedelta(seconds=min(3600,30*2**row.attempts)),
            lease_until=None,last_error=type(exc).__name__+': delivery failed')
    else:
        PartnerOutbox.objects.filter(pk=row.pk,lease_id=row.lease_id,state='sending').update(state='sent',sent_at=timezone.now(),lease_until=None,last_error='')
    return True


def schedule_reminders():
    now=timezone.now()
    for identity in PartnerIdentity.objects.filter(legacy=False).select_related('user').iterator():
        agent=getattr(identity.user,'travelagent',None)
        complete=identity.email_verified and identity.terms_at and agent and agent.payment_info_complete and not AffiliationClaim.objects.filter(agent=agent,state__in=['unmatched','ambiguous','candidate','payment_pending','rejected']).exists()
        if complete: continue
        for hours in (24,168):
            if identity.created_at <= now-timedelta(hours=hours):
                # Do not send both overdue messages together after a prolonged worker outage.
                if hours==24 and identity.created_at <= now-timedelta(hours=168): continue
                enqueue(f'setup-reminder:{identity.pk}:{hours}',identity.user.email,'Finish your Grayson partner setup',
                    'You can keep booking. Complete your email, affiliation or payment setup here:\n'+absolute_url('partner_setup'))
    for claim in AffiliationClaim.objects.filter(state__in=['unmatched','ambiguous','candidate','payment_pending','rejected'],
        created_at__lte=now-timedelta(days=7),escalated_at=None):
        with transaction.atomic():
            changed=AffiliationClaim.objects.filter(pk=claim.pk,escalated_at=None).update(escalated_at=now)
            if changed:
                enqueue(f'escalate:{claim.pk}','admin@graysontowncar.com','Partner setup needs staff attention',
                    'An affiliation or payment decision has been unresolved for seven days. No payout was released.\n'+absolute_url('partner_staff'))
