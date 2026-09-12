"""Preserve the current reporting baseline, without inventing historic membership.

No legacy claim is made visible, no verification is asserted, and no existing
commission or payout amount is changed. Uses historical models (no mail signals).
"""
from django.db import migrations
from django.utils import timezone


def seed(apps,schema_editor):
    db=schema_editor.connection.alias
    Agent=apps.get_model('users','TravelAgent');Agency=apps.get_model('users','Agency')
    Identity=apps.get_model('users','PartnerIdentity');Member=apps.get_model('users','AgencyMembership')
    Context=apps.get_model('users','PartnerBooking');Reservation=apps.get_model('reservations','Reservation')
    agents={a.pk:a for a in Agent.objects.using(db).all()}
    Identity.objects.using(db).bulk_create([Identity(user_id=a.user_id,legacy=True) for a in agents.values()],batch_size=500,ignore_conflicts=True)
    now=timezone.now()
    members={}
    for a in agents.values():
        if a.agency_id:
            members[(a.agency_id,a.user_id)]=Member(agency_id=a.agency_id,user_id=a.user_id,role='agent',booking_affiliation=True,
                payee='agency' if a.agency_handles_payment else 'direct',started_at=now)
    for agency in Agency.objects.using(db).prefetch_related('heads'):
        for head in agency.heads.all():
            key=(agency.pk,head.pk)
            if key in members: members[key].role='owner'
            else: members[key]=Member(agency_id=agency.pk,user_id=head.pk,role='owner',started_at=now)
    Member.objects.using(db).bulk_create(members.values(),batch_size=500)
    batch=[]
    for res in Reservation.objects.using(db).exclude(travel_agent=None).only('pk','travel_agent_id').iterator(chunk_size=1000):
        a=agents[res.travel_agent_id]
        inconsistent=(a.agency_handles_payment and not a.agency_id) or (a.payment_method=='agency' and not(a.agency_handles_payment and a.agency_id))
        batch.append(Context(reservation_id=res.pk,agent_id=a.pk,agency_id=a.agency_id,
            payee_agency_id=a.agency_id if a.agency_handles_payment else None,legacy=True,
            hold='Legacy payment routing requires review' if inconsistent else ''))
        if len(batch)>=1000:
            Context.objects.using(db).bulk_create(batch);batch=[]
    Context.objects.using(db).bulk_create(batch)


class Migration(migrations.Migration):
    dependencies=[('users','0034_partneroutbox_affiliationclaim_agencyalias_and_more')]
    operations=[migrations.RunPython(seed,migrations.RunPython.noop)]
