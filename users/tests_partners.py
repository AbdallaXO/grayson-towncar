"""Private matching, immutable history, payout holds, identity and outbox contracts."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError, PermissionDenied
from django.core import mail
from django.core.cache import cache
from django.db import transaction, IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse, resolve
from django.utils import timezone
from users.models import *
from users import partner_services as svc
from users.eligibility import get_commission_eligibility, STATUS_REVIEW, STATUS_READY
from users.partner_outbox import deliver_one, schedule_reminders
from users.tests_eligibility import _bootstrap, _make_reservation
from users.services import preview_agent_payout, preview_agency_payout, process_bulk_payouts


@override_settings(PARTNER_REGISTRATION_ENABLED=True, PARTNER_APPLICATIONS_ENABLED=True,
    PARTNER_MATCHING_ENABLED=True, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    TURNSTILE_SECRET='', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    # A local .env allowlist must not decide whether these tests can send.
    PARTNER_EMAIL_ALLOWLIST=[])
class PartnerTests(TestCase):
    def setUp(self):
        # Prevent unrelated booking notification threads and integrations in fixtures.
        self.mail_patch=patch('users.emails._send_email_with_retry');self.mail_patch.start();self.addCleanup(self.mail_patch.stop)
        self.vehicle,self.rate,self.customer,self.agent=_bootstrap()
        self.staff=User.objects.create_user('staff-partners',is_staff=True)
        self.owner=User.objects.create_user('owner',email='owner@bestday.test')
        self.other_owner=User.objects.create_user('other-owner',email='other@other.test')
        self.agency=Agency.objects.create(name='Best Day Ever Vacations',payment_method='zelle',payment_info='agency@example.test')
        self.other=Agency.objects.create(name='Other Agency')
        AgencyMembership.objects.create(agency=self.agency,user=self.owner,role='owner')
        AgencyMembership.objects.create(agency=self.other,user=self.other_owner,role='owner')
        self.agency.heads.add(self.owner);self.other.heads.add(self.other_owner)

    def identify(self,agent=None,verified=True,legacy=False):
        agent=agent or self.agent
        identity,_=PartnerIdentity.objects.get_or_create(user=agent.user)
        identity.legacy=legacy;identity.terms_at=timezone.now();identity.terms_version=svc.TERMS_VERSION
        if verified: identity.verified_email=agent.user.email;identity.verified_at=timezone.now()
        identity.save();return identity

    def claim(self,verified=True,payee='direct',name=None):
        self.identify(verified=verified)
        return svc.submit_claim(self.agent,name or self.agency.name,'',payee,True,self.agent.user)

    def booking(self):
        return _make_reservation(self.rate,self.customer,self.agent,status='completed')

    def register_data(self,**kwargs):
        return dict(account_type='agent',agent_name='New Agent',email='new@example.test',phone='4075550100',
            username='new-agent',password1='A very fine test pass 123!',password2='A very fine test pass 123!',
            payment_preference='direct',terms='on',**kwargs)

    def test_registration_books_without_approval(self):
        response=self.client.post(reverse('register_agent'),self.register_data())
        self.assertRedirects(response,reverse('partner_setup'))
        agent=TravelAgent.objects.get(user__username='new-agent')
        self.assertTrue(agent.is_active);self.assertIsNone(agent.agency_id)
        self.assertEqual(PartnerOutbox.objects.filter(recipient=agent.user.email).count(),2)
        self.assertEqual(self.client.get(reverse('agent_dashboard')).status_code,200)

    def test_registration_requires_server_validation(self):
        for changes in ({'terms':''},{'email':'bad'},{'phone':''},{'password2':'different'},{'username':'space here'}):
            data=self.register_data();data.update(changes)
            response=self.client.post(reverse('register_agent'),data)
            self.assertEqual(response.status_code,200)
            self.assertFalse(User.objects.filter(username='new-agent').exists())

    def test_password_validation_not_only_equality(self):
        d=self.register_data();d.update(password1='123',password2='123')
        self.assertContains(self.client.post(reverse('register_agent'),d),'password')
        self.assertFalse(User.objects.filter(username='new-agent').exists())

    def test_agency_claim_requires_disclosure(self):
        d=self.register_data(agency_name=self.agency.name)
        self.client.post(reverse('register_agent'),d)
        self.assertFalse(User.objects.filter(username='new-agent').exists())

    def test_existing_account_reused(self):
        self.client.force_login(self.agent.user)
        d=self.register_data();d['email']=self.agent.user.email
        response=self.client.post(reverse('register_agent'),d)
        self.assertRedirects(response,reverse('partner_setup'))
        self.assertEqual(TravelAgent.objects.filter(user=self.agent.user).count(),1)
        self.assertFalse(User.objects.filter(username='new-agent').exists())

    def test_passwords_not_in_resumable_session(self):
        d=self.register_data();d['terms']=''
        self.client.post(reverse('register_agent'),d)
        draft=self.client.session['partner_draft']
        self.assertNotIn('password1',draft);self.assertNotIn('password2',draft)

    def test_case_insensitive_duplicate_registration(self):
        d=self.register_data();d['email']=self.agent.user.email.upper()
        self.client.post(reverse('register_agent'),d)
        self.assertFalse(User.objects.filter(username='new-agent').exists())

    def test_unverified_claim_not_shown(self):
        c=self.claim(False);self.assertEqual(c.state,'unmatched')
        self.client.force_login(self.owner)
        self.assertNotContains(self.client.get(reverse('partner_agency',args=[self.agency.pk])),self.agent.user.email)

    def test_verified_candidate_private_to_matching_agency(self):
        c=self.claim();self.assertEqual(c.state,'candidate')
        self.client.force_login(self.owner)
        response=self.client.get(reverse('partner_agency',args=[self.agency.pk]))
        self.assertContains(response,self.agent.user.email)
        self.assertNotContains(response,self.agent.phone)
        self.assertNotContains(response,self.agent.payment_info)
        self.client.force_login(self.other_owner)
        response=self.client.get(reverse('partner_agency',args=[self.other.pk]))
        self.assertNotContains(response,self.agent.user.email)

    def test_candidate_cannot_see_workspace(self):
        self.claim();self.client.force_login(self.agent.user)
        self.assertEqual(self.client.get(reverse('partner_agency',args=[self.agency.pk])).status_code,403)

    def test_cross_agency_confirmation_denied(self):
        c=self.claim()
        with self.assertRaises(PermissionDenied): svc.review_candidate(self.other_owner,c.pk,'add','direct')

    def test_stated_name_not_domain_controls_candidates(self):
        self.agent.user.email='person@bestday.test';self.agent.user.save()
        self.identify()
        svc.rematch_claims()
        self.assertFalse(AffiliationClaim.objects.exists())
        c=svc.submit_claim(self.agent,self.other.name,'','direct',True,self.agent.user)
        self.assertEqual(c.agency_id,self.other.pk)

    def test_personal_email_allowed(self):
        self.agent.user.email='myagent@gmail.com';self.agent.user.save()
        self.assertEqual(self.claim().agency_id,self.agency.pk)

    def test_alias_matching_and_ambiguity(self):
        AgencyAlias.objects.create(agency=self.agency,name='  BEST DAY EVER  ')
        c=self.claim(name='best day ever');self.assertEqual(c.agency_id,self.agency.pk)
        AgencyAlias.objects.create(agency=self.other,name='Best day ever')
        svc.match_claim(c);c.refresh_from_db()
        self.assertEqual(c.state,'ambiguous');self.assertIsNone(c.agency_id)

    def test_typo_not_routed(self):
        c=self.claim(name='Best Dey Ever Vacations')
        self.assertEqual(c.state,'unmatched')

    def test_name_collision_not_guessed(self):
        Agency.objects.create(name=self.agency.name)
        self.assertEqual(self.claim().state,'ambiguous')

    def test_email_change_hides_candidate(self):
        self.claim();self.agent.user.email='changed@test.test';self.agent.user.save()
        self.client.force_login(self.owner)
        self.assertNotContains(self.client.get(reverse('partner_agency',args=[self.agency.pk])),'changed@test.test')

    def test_candidate_has_no_guest_history(self):
        self.claim();self.booking();self.client.force_login(self.owner)
        self.assertNotContains(self.client.get(reverse('partner_agency',args=[self.agency.pk])),self.customer.get_full_name())

    def test_approval_preserves_old_visibility(self):
        self.identify();old=self.booking();c=self.claim()
        svc.review_candidate(self.owner,c.pk,'add','direct')
        old.partner_context.refresh_from_db();self.assertIsNone(old.partner_context.agency_id)
        new=self.booking();self.assertEqual(new.partner_context.agency_id,self.agency.pk)

    def test_pending_commission_accrues_but_cannot_pay(self):
        self.claim();r=self.booking();e=get_commission_eligibility(r)
        self.assertEqual(e.status,STATUS_REVIEW);self.assertEqual(e.commission,Decimal('10'))
        payout,total,_=self.agent.process_commission_payment();self.assertIsNone(payout)
        self.assertEqual(preview_agent_payout(self.agent)['count'],0)
        r.refresh_from_db();self.assertFalse(r.commission_paid)

    def test_matching_payee_resolves_only_claim_bookings(self):
        c=self.claim();r=self.booking()
        svc.review_candidate(self.owner,c.pk,'add','direct')
        r.refresh_from_db();self.assertEqual(get_commission_eligibility(r).status,STATUS_READY)
        self.assertIsNone(r.partner_context.agency_id)
        self.assertFalse(r.partner_context.hold)

    def test_changed_payee_requires_acceptance(self):
        c=self.claim();r=self.booking()
        svc.review_candidate(self.owner,c.pk,'add','agency');c.refresh_from_db()
        self.assertEqual(c.state,'payment_pending');r.refresh_from_db()
        self.assertEqual(get_commission_eligibility(r).status,STATUS_REVIEW)
        svc.accept_payment(self.agent.user,c.pk);r.refresh_from_db()
        self.assertEqual(r.partner_context.payee_agency_id,self.agency.pk)
        self.assertIsNone(r.partner_context.agency_id)
        self.assertEqual(preview_agent_payout(self.agent)['count'],0)
        self.assertEqual(preview_agency_payout(self.agency)['count'],1)

    def test_reject_does_not_delete_account_or_release_payout(self):
        c=self.claim();r=self.booking();svc.review_candidate(self.owner,c.pk,'reject')
        self.assertTrue(User.objects.filter(pk=self.agent.user_id).exists())
        r.refresh_from_db();self.assertEqual(get_commission_eligibility(r).status,STATUS_REVIEW)
        self.client.force_login(self.agent.user);self.assertEqual(self.client.get(reverse('agent_dashboard')).status_code,200)

    def test_repeat_approval_idempotent(self):
        c=self.claim();svc.review_candidate(self.owner,c.pk,'add','direct');svc.review_candidate(self.owner,c.pk,'add','direct')
        self.assertEqual(AgencyMembership.objects.filter(user=self.agent.user,active=True).count(),1)

    def test_remove_preserves_old_agency_and_future_independent(self):
        c=self.claim(payee='agency');svc.review_candidate(self.owner,c.pk,'add','agency')
        r=self.booking();m=AgencyMembership.objects.get(user=self.agent.user,active=True)
        svc.remove_member(self.owner,m.pk,'Agent left agency')
        r.partner_context.refresh_from_db();self.assertEqual(r.partner_context.agency_id,self.agency.pk)
        new=self.booking();self.assertIsNone(new.partner_context.agency_id)
        self.assertEqual(preview_agency_payout(self.agency)['count'],1)

    def test_agency_payments_include_former_member(self):
        c=self.claim(payee='agency');svc.review_candidate(self.owner,c.pk,'add','agency');r=self.booking()
        svc.remove_member(self.owner,AgencyMembership.objects.get(user=self.agent.user,active=True).pk,'Left')
        payout,total=self.agency.process_agency_commission_payment()
        self.assertEqual(total,Decimal('10'));self.assertIsNotNone(payout)
        r.refresh_from_db();self.assertTrue(r.commission_paid)
        self.assertIsNone(self.agency.process_agency_commission_payment()[0])

    def test_direct_payout_never_pays_agency_group(self):
        c=self.claim(payee='agency');svc.review_candidate(self.owner,c.pk,'add','agency');self.booking()
        self.assertIsNone(self.agent.process_commission_payment()[0])

    def test_last_owner_cannot_be_removed(self):
        m=AgencyMembership.objects.get(user=self.owner)
        with self.assertRaises(ValidationError):svc.remove_member(self.staff,m.pk,'Removal')

    def test_admin_cannot_promote_to_owner(self):
        m=AgencyMembership.objects.get(user=self.other_owner)
        m.agency=self.agency;m.role='admin';m.save()
        with self.assertRaises(PermissionDenied):svc.change_role(self.other_owner,m.pk,'owner','Take ownership')

    def test_multi_agency_manager_selects_workspace(self):
        AgencyMembership.objects.create(user=self.owner,agency=self.other,role='admin')
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('agency_profile')).status_code,200)
        self.assertEqual(self.client.get(reverse('partner_agency',args=[self.other.pk])).status_code,200)

    def test_applicant_not_owner_before_staff_approval(self):
        self.identify();app=AgencyApplication.objects.create(applicant=self.agent.user,name='New Agency',authority='Owner')
        self.assertFalse(svc.managed_agencies(self.agent.user).exists())
        agency=svc.review_application(self.staff,app.pk,'approved','Verified business contact')
        self.assertTrue(svc.managed_agencies(self.agent.user).filter(pk=agency.pk).exists())
        self.assertEqual(AgencyMembership.objects.get(user=self.agent.user,agency=agency).role,'owner')
        self.assertEqual(svc.review_application(self.staff,app.pk,'approved','Repeat').pk,agency.pk)

    def test_unverified_owner_cannot_be_approved(self):
        self.identify(verified=False);app=AgencyApplication.objects.create(applicant=self.agent.user,name='New Agency',authority='Owner')
        with self.assertRaises(ValidationError):svc.review_application(self.staff,app.pk,'approved','Checked')

    def test_duplicate_agency_application_requires_explicit_link(self):
        self.identify();app=AgencyApplication.objects.create(applicant=self.agent.user,name=self.agency.name,authority='Owner')
        with self.assertRaises(ValidationError):svc.review_application(self.staff,app.pk,'approved','Checked')
        agency=svc.review_application(self.staff,app.pk,'approved','Confirmed ownership',self.agency.pk)
        self.assertEqual(agency,self.agency)

    def test_legacy_affiliation_not_disclosed(self):
        self.agent.agency_name=self.agency.name;self.agent.save()
        svc.rematch_claims();self.assertFalse(self.agency.claims.exists())

    def test_backfill_is_previewed_and_paid_records_protected(self):
        self.identify();r=self.booking()
        p=svc.preview_backfill(self.staff,[r.pk],self.agency,True,False,'direct','Historical association confirmed')
        r.partner_context.refresh_from_db();self.assertIsNone(r.partner_context.agency_id)
        svc.apply_backfill(self.staff,p.pk);r.partner_context.refresh_from_db();self.assertEqual(r.partner_context.agency_id,self.agency.pk)
        self.agent.process_commission_payment()
        with self.assertRaises(ValidationError):svc.preview_backfill(self.staff,[r.pk],self.agency,False,True,'agency','Change payee')

    def test_stale_backfill_rejected(self):
        self.identify();r=self.booking();p=svc.preview_backfill(self.staff,[r.pk],self.agency,True,False,'direct','Test')
        PartnerBooking.objects.filter(reservation=r).update(hold='Changed')
        with self.assertRaises(ValidationError):svc.apply_backfill(self.staff,p.pk)

    def test_backfill_requires_same_staff_actor(self):
        self.identify();r=self.booking();p=svc.preview_backfill(self.staff,[r.pk],self.agency,True,False,'direct','Test')
        with self.assertRaises(PartnerBackfill.DoesNotExist):svc.apply_backfill(self.owner,p.pk)

    def test_verification_bound_to_email_and_single_use(self):
        self.identify(verified=False);svc.issue_verification(self.agent.user)
        raw=PartnerOutbox.objects.get(key__startswith='verify:').body.split('/verify/')[1].split('/')[0]
        svc.verify_email(raw);svc.verify_email(raw)
        self.assertTrue(svc.email_verified(self.agent.user))
        self.agent.user.email='different@test.test';self.agent.user.save()
        with self.assertRaises(ValidationError):svc.verify_email(raw)

    def test_verification_transaction_rollback_does_not_queue(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                svc.issue_verification(self.agent.user)
                raise RuntimeError('rollback')
        self.assertFalse(PartnerOutbox.objects.exists())

    def test_unverified_email_cannot_attribute_booking(self):
        from reservations.attribution import resolve_agent_by_customer_email
        self.customer.email=self.agent.user.email;self.customer.save()
        class Stub: pass
        r=Stub();r.customer=self.customer;r.customer_id=self.customer.pk
        self.assertIsNone(resolve_agent_by_customer_email(r))
        self.identify();self.assertEqual(resolve_agent_by_customer_email(r),self.agent)
        other=User.objects.create_user('duplicate',email=self.agent.user.email)
        TravelAgent.objects.create(user=other,phone='555')
        self.assertIsNone(resolve_agent_by_customer_email(r))

    def test_outbox_idempotent_and_retryable(self):
        svc.enqueue('test','test@example.test','Test','Body');svc.enqueue('test','test@example.test','Test','Body')
        self.assertEqual(PartnerOutbox.objects.count(),1)
        with patch('users.partner_outbox.EmailMultiAlternatives.send',side_effect=RuntimeError('Fail')):deliver_one()
        row=PartnerOutbox.objects.get();self.assertEqual(row.state,'pending');self.assertEqual(row.attempts,1)
        row.available_at=timezone.now();row.save()
        self.assertTrue(deliver_one());row.refresh_from_db();self.assertEqual(row.state,'sent')
        self.assertFalse(deliver_one())

    def test_abandoned_lease_is_recovered(self):
        row=svc.enqueue('lease','test@example.test','Test','Body')
        row.state='sending';row.lease_until=timezone.now()-timedelta(minutes=1);row.save()
        self.assertTrue(deliver_one());row.refresh_from_db();self.assertEqual(row.state,'sent')

    def test_reminders_deduplicated_and_completed_setup_stops(self):
        i=self.identify(verified=False);i.created_at=timezone.now()-timedelta(days=2);i.save()
        schedule_reminders();schedule_reminders()
        self.assertEqual(PartnerOutbox.objects.filter(key__startswith='setup-reminder').count(),1)
        self.identify();PartnerOutbox.objects.all().delete();schedule_reminders()
        self.assertFalse(PartnerOutbox.objects.exists())

    def test_staff_and_payout_screens_render(self):
        self.claim();self.booking();self.client.force_login(self.staff)
        for name in ['partner_staff','partner_payouts']:
            self.assertEqual(self.client.get(reverse(name)).status_code,200)

    def test_feature_pause_keeps_booking_and_setup(self):
        self.claim();self.client.force_login(self.agent.user)
        with override_settings(PARTNER_MATCHING_ENABLED=False,PARTNER_APPLICATIONS_ENABLED=False):
            self.assertEqual(self.client.get(reverse('partner_setup')).status_code,200)
            self.assertEqual(self.client.get(reverse('agent_dashboard')).status_code,200)

    def test_legacy_agency_detail_route_is_private_workspace(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('agency_detail',args=[self.agency.pk])).status_code,200)

    def test_claim_withdrawal_does_not_clear_old_holds(self):
        self.claim();r=self.booking()
        svc.submit_claim(self.agent,self.other.name,'','direct',True,self.agent.user)
        r.refresh_from_db();self.assertTrue(r.partner_context.hold)

    def test_inquiry_conversion_preserves_prefill(self):
        inquiry=PartnerForm.objects.create(name='Lead',email='lead@example.test',phone_number='4075550100',agency_name='')
        raw=PartnerOutbox.objects.get(key=f'inquiry:{inquiry.pk}').body.split('/continue/')[1].split('/')[0]
        self.client.get(reverse('partner_continue',args=[raw]))
        d=self.register_data();d['email']=inquiry.email
        self.client.post(reverse('register_agent'),d)
        inquiry.refresh_from_db();self.assertEqual(inquiry.status,'converted')

    def test_staff_cannot_assign_inactive_agency(self):
        self.agency.is_active=False;self.agency.save()
        with self.assertRaises(ValidationError):
            svc.assign_member(self.staff,self.agent,self.agency,'direct','Confirmed affiliation')

    def test_admin_cannot_edit_agency_payout_details(self):
        AgencyMembership.objects.filter(user=self.owner).update(role='admin')
        self.client.force_login(self.owner)
        response=self.client.post(reverse('partner_agency',args=[self.agency.pk]),dict(action='profile',phone='',address='',website='',payment_method='zelle',payment_info='changed'))
        self.assertEqual(response.status_code,403)
        self.agency.refresh_from_db();self.assertEqual(self.agency.payment_info,'agency@example.test')

    def test_backfill_rejects_commission_rate_change_after_preview(self):
        r=self.booking()
        preview=svc.preview_backfill(self.staff,[r.pk],self.agency,True,False,'direct','Confirmed history')
        TravelAgent.objects.filter(pk=self.agent.pk).update(commission_rate=Decimal('12'))
        with self.assertRaises(ValidationError): svc.apply_backfill(self.staff,preview.pk)

    def test_agency_statement_excludes_unattributed_guest(self):
        claim=self.claim(payee='agency');r=self.booking()
        svc.review_candidate(self.owner,claim.pk,'add','agency')
        payout,_=svc.process_recorded_agency_payout(self.agency)
        self.client.force_login(self.owner)
        response=self.client.get(reverse('agency_commission_payout_detail',args=[payout.pk]))
        self.assertEqual(response.status_code,200)
        self.assertEqual(list(response.context['rows']),[])

    def test_draft_endpoint_preserves_only_nonsecret_fields(self):
        self.client.post(reverse('partner_draft'),self.register_data())
        draft=self.client.session['partner_draft']
        self.assertEqual(draft['agent_name'],'New Agent')
        self.assertNotIn('password1',draft);self.assertNotIn('terms',draft)

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.db import connection, close_old_connections
from django.test import TransactionTestCase
from unittest import skipUnless


@skipUnless(connection.vendor=='postgresql','PostgreSQL row-lock contracts require PostgreSQL')
@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class PartnerConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.mail_patch=patch('users.emails._send_email_with_retry');self.mail_patch.start();self.addCleanup(self.mail_patch.stop)
        self.vehicle,self.rate,self.customer,self.agent=_bootstrap()
        self.owner=User.objects.create_user('concurrent-owner',email='owner@test.test')
        self.agency=Agency.objects.create(name='Concurrency Agency',payment_method='zelle',payment_info='test')
        AgencyMembership.objects.create(user=self.owner,agency=self.agency,role='owner')
        PartnerIdentity.objects.create(user=self.agent.user,verified_email=self.agent.user.email,verified_at=timezone.now(),terms_at=timezone.now())

    def race(self,fn):
        barrier=Barrier(2)
        def run():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return fn()
            finally: close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=[pool.submit(run) for _ in range(2)]
            return [r.result(timeout=30) for r in results]

    def test_double_approval_creates_one_membership(self):
        claim=svc.submit_claim(self.agent,self.agency.name,'','direct',True,self.agent.user)
        self.race(lambda: svc.review_candidate(User.objects.get(pk=self.owner.pk),claim.pk,'add','direct'))
        self.assertEqual(AgencyMembership.objects.filter(user=self.agent.user,active=True).count(),1)
        self.assertEqual(PartnerEvent.objects.filter(kind='candidate_add',object_id=claim.pk).count(),1)

    def test_double_payout_records_money_once(self):
        reservation=_make_reservation(self.rate,self.customer,self.agent,status='completed')
        results=self.race(lambda: TravelAgent.objects.get(pk=self.agent.pk).process_commission_payment()[1])
        self.assertEqual(sum(results),Decimal('10'))
        self.assertEqual(CommissionPayout.objects.filter(agent=self.agent).count(),1)
        reservation.refresh_from_db();self.assertTrue(reservation.commission_paid)

    def test_same_application_approval_creates_one_agency(self):
        staff=User.objects.create_user('concurrent-staff',is_staff=True)
        app=AgencyApplication.objects.create(applicant=self.agent.user,name='Concurrent new business',authority='Owner')
        results=self.race(lambda: svc.review_application(User.objects.get(pk=staff.pk),app.pk,'approved','Verified contact').pk)
        self.assertEqual(len(set(results)),1)
        self.assertEqual(Agency.objects.filter(name=app.name).count(),1)

    def test_concurrent_identity_registration_reuses_no_arbitrary_user(self):
        def register():
            try:
                return svc.create_personal_user('concurrent-registration','new-identity@example.test','test password').pk
            except ValidationError:
                return None
        results=self.race(register)
        self.assertEqual(sum(pk is not None for pk in results),1)
        self.assertEqual(User.objects.filter(email='new-identity@example.test').count(),1)


@override_settings(PARTNER_REGISTRATION_ENABLED=True, PARTNER_PAYOUT_KEY='',
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class PayoutDetailsTests(TestCase):
    """Structured payout details: encrypted at rest, validated per rail, and
    retired rails kept readable for the agents already on them."""

    def setUp(self):
        self.mail_patch=patch('users.emails._send_email_with_retry');self.mail_patch.start();self.addCleanup(self.mail_patch.stop)
        self.vehicle,self.rate,self.customer,self.agent=_bootstrap()

    def form(self,**overrides):
        from users.partner_forms import PartnerProfileForm
        data=dict(agent_name='A Agent',phone='4075550100',payment_method='bank',
            bank_account_name='A Agent',bank_routing_number='021000021',
            bank_account_type='checking',bank_account_number='4321876500')
        data.update(overrides)
        return PartnerProfileForm(data,instance=self.agent)

    def test_account_number_is_encrypted_not_stored_in_clear(self):
        form=self.form();self.assertTrue(form.is_valid(),form.errors)
        agent=form.save();agent.refresh_from_db()
        self.assertNotIn('4321876500',agent.bank_account_encrypted)
        self.assertEqual(agent.bank_account_last4,'6500')
        self.assertEqual(agent.bank_account_number,'4321876500')

    def test_routing_checksum_rejects_a_typo(self):
        form=self.form(bank_routing_number='021000022')
        self.assertFalse(form.is_valid())
        self.assertIn('bank_routing_number',form.errors)

    def test_each_rail_requires_only_its_own_field(self):
        form=self.form(payment_method='paypal',paypal_email='pay@example.test',
            bank_account_name='',bank_routing_number='',bank_account_type='',bank_account_number='')
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.save().payout_target('paypal'),'pay@example.test')

    def test_venmo_handle_is_normalised_and_checked(self):
        form=self.form(payment_method='venmo',venmo_handle='@Rebekah_L',
            bank_account_name='',bank_routing_number='',bank_account_type='',bank_account_number='')
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.save().venmo_handle,'Rebekah_L')
        self.assertFalse(self.form(payment_method='venmo',venmo_handle='no spaces here',
            bank_account_name='',bank_routing_number='',bank_account_type='',bank_account_number='').is_valid())

    def test_retired_rails_are_not_offered_to_new_partners(self):
        offered=[k for k,_ in self.form().fields['payment_method'].choices]
        for retired in ['zelle','cashapp','check','other']:
            self.assertNotIn(retired,offered)
        for kept in ['paypal','venmo','bank']:
            self.assertIn(kept,offered)

    def test_an_agent_already_on_a_retired_rail_keeps_it(self):
        self.agent.payment_method='zelle';self.agent.payment_info='agent@zelle.test';self.agent.save()
        form=self.form(payment_method='zelle')
        self.assertIn('zelle',[k for k,_ in form.fields['payment_method'].choices])
        self.assertTrue(form.is_valid(),form.errors)
        # Legacy free text still answers "where does this person get paid?"
        self.assertEqual(self.agent.payout_target('zelle'),'agent@zelle.test')

    def test_existing_account_number_survives_an_unrelated_edit(self):
        self.form().save()
        form=self.form(agent_name='Renamed Agent',bank_account_number='')
        self.assertTrue(form.is_valid(),form.errors)
        agent=form.save();agent.refresh_from_db()
        self.assertEqual(agent.agent_name,'Renamed Agent')
        self.assertEqual(agent.bank_account_number,'4321876500')

    def test_unreadable_ciphertext_does_not_crash_a_payout_run(self):
        form=self.form();form.is_valid();agent=form.save()
        agent.bank_account_encrypted='not-a-valid-token';agent.save()
        self.assertEqual(agent.bank_account_number,'')
        self.assertEqual(agent.bank_account_masked,'••••6500')


@override_settings(PARTNER_REGISTRATION_ENABLED=True, PARTNER_MATCHING_ENABLED=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', TURNSTILE_SECRET='',
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class PayoutAtRegistrationTests(TestCase):
    """Direct-paid partners can finish payout setup during signup; agency-paid
    partners are never asked, and a half-filled rail is refused."""

    def setUp(self):
        self.mail_patch=patch('users.emails._send_email_with_retry');self.mail_patch.start();self.addCleanup(self.mail_patch.stop)
        # Registration is IP rate-limited through the cache, which outlives a
        # test's transaction; without this, later tests are throttled not failed.
        cache.clear()

    def data(self,**kwargs):
        base=dict(account_type='agent',agent_name='New Agent',email='new@example.test',phone='4075550100',
            username='new-agent',password1='A very fine test pass 123!',password2='A very fine test pass 123!',
            payment_preference='direct',terms='on')
        base.update(kwargs)
        return base

    def test_direct_agent_can_supply_paypal_at_signup(self):
        response=self.client.post(reverse('register_agent'),self.data(payment_method='paypal',paypal_email='pay@example.test'))
        self.assertRedirects(response,reverse('partner_setup'))
        agent=TravelAgent.objects.get(user__username='new-agent')
        self.assertEqual(agent.payment_method,'paypal')
        self.assertEqual(agent.payout_target('paypal'),'pay@example.test')

    def test_bank_details_are_encrypted_from_signup(self):
        self.client.post(reverse('register_agent'),self.data(payment_method='bank',bank_account_name='New Agent',
            bank_routing_number='021000021',bank_account_type='checking',bank_account_number='4321876500'))
        agent=TravelAgent.objects.get(user__username='new-agent')
        self.assertEqual(agent.bank_account_last4,'6500')
        self.assertNotIn('4321876500',agent.bank_account_encrypted)
        self.assertEqual(agent.bank_account_number,'4321876500')

    def test_payout_details_stay_optional(self):
        self.assertRedirects(self.client.post(reverse('register_agent'),self.data()),reverse('partner_setup'))
        agent=TravelAgent.objects.get(user__username='new-agent')
        self.assertFalse(agent.payment_method)
        self.assertFalse(agent.payment_info_complete)

    def test_half_filled_rail_is_refused(self):
        response=self.client.post(reverse('register_agent'),self.data(payment_method='paypal',paypal_email=''))
        self.assertEqual(response.status_code,200)
        self.assertFalse(User.objects.filter(username='new-agent').exists())

    def test_agency_paid_partner_is_never_asked(self):
        agency=Agency.objects.create(name='Best Day Ever Vacations')
        self.client.post(reverse('register_agent'),self.data(payment_preference='agency',agency_name=agency.name,
            disclosure='on',payment_method='paypal',paypal_email='sneaky@example.test'))
        agent=TravelAgent.objects.get(user__username='new-agent')
        # The rail they cannot use must not be quietly stored against them.
        self.assertFalse(agent.payment_method)
        self.assertFalse(agent.paypal_email)

    def test_account_number_never_lands_in_the_session_draft(self):
        self.client.post(reverse('register_agent'),self.data(terms='',payment_method='bank',
            bank_account_name='New Agent',bank_routing_number='021000021',
            bank_account_type='checking',bank_account_number='4321876500'))
        draft=self.client.session.get('partner_draft',{})
        self.assertNotIn('bank_account_number',draft)
        self.assertNotIn('4321876500',str(draft))


@override_settings(PARTNER_MATCHING_ENABLED=True, PARTNER_REGISTRATION_ENABLED=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AgencyPayoutPolicyTests(TestCase):
    """An agency that pays its own agents is always the payee, whatever an agent
    asked for on the way in."""

    def setUp(self):
        self.mail_patch=patch('users.emails._send_email_with_retry');self.mail_patch.start();self.addCleanup(self.mail_patch.stop)
        cache.clear()
        self.vehicle,self.rate,self.customer,self.agent=_bootstrap()
        self.owner=User.objects.create_user('policy-owner',email='owner@policy.test')
        self.agency=Agency.objects.create(name='Policy Agency',payout_policy='agency')
        AgencyMembership.objects.create(agency=self.agency,user=self.owner,role='owner')
        self.agency.heads.add(self.owner)

    def claim(self,requested):
        identity,_=PartnerIdentity.objects.get_or_create(user=self.agent.user)
        identity.verified_email=self.agent.user.email;identity.verified_at=timezone.now()
        identity.terms_at=timezone.now();identity.save()
        return svc.submit_claim(self.agent,self.agency.name,'',requested,True,self.agent.user)

    def test_agency_only_policy_blocks_a_direct_approval(self):
        claim=self.claim('direct')
        self.assertEqual(claim.state,'candidate')
        with self.assertRaises(ValidationError):
            svc.review_candidate(self.owner,claim.pk,'add','direct')
        claim.refresh_from_db()
        self.assertEqual(claim.state,'candidate')
        self.assertFalse(AgencyMembership.objects.filter(agency=self.agency,user=self.agent.user).exists())

    def test_agency_payee_is_approved_and_the_agent_must_accept(self):
        claim=self.claim('direct')
        svc.review_candidate(self.owner,claim.pk,'add','agency')
        claim.refresh_from_db()
        # Asked for direct, offered agency: the agent has to agree before it counts.
        self.assertEqual(claim.state,'payment_pending')
        self.assertEqual(claim.offered_payee,'agency')
        membership=AgencyMembership.objects.get(agency=self.agency,user=self.agent.user)
        self.assertEqual(membership.payee,'pending')

    def test_an_open_agency_may_still_approve_direct(self):
        self.agency.payout_policy='either';self.agency.save()
        claim=self.claim('direct')
        svc.review_candidate(self.owner,claim.pk,'add','direct')
        claim.refresh_from_db()
        self.assertEqual(claim.state,'approved')
        self.assertEqual(AgencyMembership.objects.get(agency=self.agency,user=self.agent.user).payee,'direct')


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class OutboxAllowlistTests(TestCase):
    """Testing runs against a copy of production. The allowlist must stop mail
    reaching a real agent or agency, whatever queued it."""

    @override_settings(PARTNER_EMAIL_ALLOWLIST=['me@graysontowncar.com'])
    def test_a_real_recipient_is_held_not_sent(self):
        svc.enqueue('candidate:test','rachel@magicaltravel.com','Someone named your agency','body')
        self.assertTrue(deliver_one())
        row=PartnerOutbox.objects.get(key='candidate:test')
        self.assertEqual(row.state,'held')
        self.assertEqual(row.attempts,1)
        self.assertIsNone(row.sent_at)
        self.assertEqual(len(mail.outbox),0)

    @override_settings(PARTNER_EMAIL_ALLOWLIST=['me@graysontowncar.com'])
    def test_an_allowed_recipient_still_receives(self):
        svc.enqueue('verify:test','ME@graysontowncar.com','Verify your email','body')
        self.assertTrue(deliver_one())
        self.assertEqual(PartnerOutbox.objects.get(key='verify:test').state,'sent')
        self.assertEqual(len(mail.outbox),1)

    @override_settings(PARTNER_EMAIL_ALLOWLIST=[])
    def test_an_empty_allowlist_does_not_restrict_production(self):
        svc.enqueue('verify:prod','someone@example.test','Verify your email','body')
        self.assertTrue(deliver_one())
        self.assertEqual(PartnerOutbox.objects.get(key='verify:prod').state,'sent')
        self.assertEqual(len(mail.outbox),1)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class LoginIdentifierTests(TestCase):
    """Sign in with a username or an email — but never guess when either is
    shared, because this database really does contain duplicates."""

    def setUp(self):
        cache.clear()
        self.user=User.objects.create_user('jane-agent',email='jane@agency.test',password='a good test password')

    def resolve(self,value):
        from users.views import resolve_login_identifier
        return resolve_login_identifier(value)

    def test_username_resolves(self):
        self.assertEqual(self.resolve('jane-agent'),(self.user,False))

    def test_username_is_case_insensitive(self):
        self.assertEqual(self.resolve('JANE-Agent'),(self.user,False))

    def test_email_resolves(self):
        self.assertEqual(self.resolve('JANE@agency.test'),(self.user,False))

    def test_unknown_identifier_is_not_ambiguous(self):
        self.assertEqual(self.resolve('nobody@nowhere.test'),(None,False))

    def test_shared_email_refuses_rather_than_guessing(self):
        User.objects.create_user('jane-second',email='jane@agency.test',password='another password')
        user,ambiguous=self.resolve('jane@agency.test')
        self.assertIsNone(user)
        self.assertTrue(ambiguous)

    def test_case_variant_usernames_refuse_rather_than_guessing(self):
        User.objects.create_user('Jane-Agent',email='other@agency.test',password='another password')
        user,ambiguous=self.resolve('jane-agent')
        self.assertIsNone(user)
        self.assertTrue(ambiguous)

    def test_a_username_wins_over_someone_elses_email(self):
        # One person's username is another's email local-part; the username owns it.
        User.objects.create_user('someone',email='jane-agent',password='another password')
        self.assertEqual(self.resolve('jane-agent'),(self.user,False))

    def test_signing_in_by_email_actually_works(self):
        TravelAgent.objects.create(user=self.user,phone='4075550100')
        response=self.client.post(reverse('agent_login'),
            {'username':'jane@agency.test','password':'a good test password'})
        self.assertEqual(response.status_code,302)
        self.assertEqual(int(self.client.session['_auth_user_id']),self.user.pk)

    def test_shared_email_login_is_refused_at_the_view(self):
        User.objects.create_user('jane-second',email='jane@agency.test',password='a good test password')
        response=self.client.post(reverse('agent_login'),
            {'username':'jane@agency.test','password':'a good test password'},follow=True)
        self.assertNotIn('_auth_user_id',self.client.session)
        self.assertContains(response,'shared by more than one account')
