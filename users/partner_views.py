"""Private partner registration and agency workspaces; no public directories."""
import hashlib
import json
from decimal import Decimal, InvalidOperation
from datetime import timedelta, date
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError
from django.db.models import Q, Count, Sum
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from .models import *
from .partner_forms import PartnerRegistrationForm, PartnerProfileForm, AffiliationForm, AgencyProfileForm
from . import partner_services as svc
from . import turnstile


def limited(request, scope, limit=10, seconds=3600):
    # REMOTE_ADDR is trusted here; never trust arbitrary forwarded headers.
    key='partner:'+scope+':'+hashlib.sha256((request.META.get('REMOTE_ADDR','')+':'+str(request.user.pk or '')).encode()).hexdigest()
    if cache.add(key,1,seconds): return False
    try: return cache.incr(key)>limit
    except ValueError: return False


def _error(request, error):
    messages.error(request,' '.join(error.messages) if isinstance(error,ValidationError) else 'Please check your selection and try again.')


def register(request):
    if not getattr(settings,'PARTNER_REGISTRATION_ENABLED',True):
        return HttpResponse('Partner registration is temporarily paused. Please contact Grayson.',status=503)
    draft=request.session.get('partner_draft',{})
    initial={'account_type':request.GET.get('type','agent'),'payment_preference':'direct',**draft}
    if initial['account_type']=='agency' and not draft:
        initial['payment_preference']='agency'
    if request.user.is_authenticated:
        agent=TravelAgent.objects.filter(user=request.user).first()
        initial.update(email=request.user.email,agent_name=agent.agent_name if agent else request.user.get_full_name(),phone=agent.phone if agent else '')
    form=PartnerRegistrationForm(request.POST or None,initial=initial,user=request.user)
    if request.method=='POST':
        # Never persist passwords in sessions or resumable URLs.
        request.session['partner_draft']={k:v for k,v in request.POST.items() if k in form.fields and k not in ['password1','password2','terms','disclosure','bank_account_number']}
        if limited(request,'register'):
            form.add_error(None,'Too many attempts. Please try again later.')
        else:
            passed,_=turnstile.verify_request(request,request.META.get('REMOTE_ADDR'))
            if not passed: form.add_error(None,'Please complete the verification challenge and try again.')
            elif form.is_valid():
                try:
                    with transaction.atomic():
                        data=form.cleaned_data
                        if request.user.is_authenticated:
                            user=User.objects.select_for_update().get(pk=request.user.pk)
                        else:
                            user=svc.create_personal_user(data['username'],data['email'],data['password1'])
                        identity,new_identity=PartnerIdentity.objects.get_or_create(user=user, defaults={'registration_email':user.email.strip().lower()})
                        identity.terms_version,identity.terms_at=svc.TERMS_VERSION,timezone.now()
                        inquiry_id=request.session.get('partner_inquiry')
                        if inquiry_id:
                            inquiry=PartnerForm.objects.filter(pk=inquiry_id,email__iexact=user.email).first()
                            if inquiry:
                                identity.inquiry=inquiry
                                inquiry.status='converted';inquiry.save(update_fields=['status'])
                        identity.save()
                        agent,new_agent=TravelAgent.objects.get_or_create(user=user,defaults=dict(agent_name=data['agent_name'],phone=data['phone']))
                        if form.payout_chosen():
                            # Captured at signup so a direct-paid agent is ready
                            # the first time a commission becomes payable.
                            agent.payment_method=data['payment_method']
                            form.apply_payout_fields(agent)
                            agent.save()
                        UserProfile.objects.get_or_create(user=user,defaults={'phone_number':data['phone'],'is_travel_agent':True})
                        UserProfile.objects.filter(user=user).update(is_travel_agent=True)
                        if data['agency_name']:
                            svc.submit_claim(agent,data['agency_name'],data['agency_website'],data['payment_preference'],data['disclosure'],user)
                        if data['account_type']=='agency':
                            if not getattr(settings,'PARTNER_APPLICATIONS_ENABLED',False):
                                raise ValidationError('New agency applications are paused. Please contact Grayson.')
                            app,created=AgencyApplication.objects.get_or_create(applicant=user,state__in=['pending','information'],defaults=dict(state='pending',
                                name=data['agency_name'],website=data['agency_website'],phone=data['phone'],address=data['address'],authority=data['authority']))
                            if created:
                                svc.event(user,'agency_applied',app)
                                svc.enqueue(f'application:{app.pk}','admin@graysontowncar.com','Agency application ready for review',svc.absolute_url('partner_staff'))
                        svc.issue_verification(user)
                        if new_identity:
                            svc.enqueue(f'welcome:{user.pk}',user.email,'Welcome to Grayson',
                                'Your partner account is ready and you can book immediately. Complete your setup here:\n'+svc.absolute_url('partner_setup'))
                    login(request,user,backend='django.contrib.auth.backends.ModelBackend')
                    request.session.pop('partner_draft',None);request.session.pop('partner_inquiry',None)
                    messages.success(request,'Your account is ready. You can book now while completing partner setup.')
                    return redirect('partner_setup')
                except ValidationError as e: form.add_error(None,e)
                except IntegrityError: form.add_error(None,'This account or application already exists. Sign in to continue.')
    return render(request,'users/partners/register.html',{'form':form,'turnstile_site_key':turnstile.site_key(),'terms_version':svc.TERMS_VERSION})


def continue_inquiry(request,token):
    record=get_object_or_404(PartnerToken,digest=hashlib.sha256(token.encode()).hexdigest(),purpose='inquiry',expires_at__gt=timezone.now())
    inquiry=record.inquiry
    request.session['partner_inquiry']=inquiry.pk
    request.session['partner_draft']=dict(agent_name=inquiry.name,email=inquiry.email,phone=inquiry.phone_number,
        agency_name=inquiry.agency_name,agency_website=inquiry.agency_website or '')
    return redirect('register_agent')


def verify(request,token):
    if request.method=='POST':
        if limited(request,'verify',30): return HttpResponse('Please try again later.',status=429)
        try:
            svc.verify_email(token)
            messages.success(request,'Email verified. Sign in to continue your partner setup.')
            return redirect('partner_setup' if request.user.is_authenticated else 'agent_login')
        except (ValidationError,ValueError) as e: _error(request,e)
    return render(request,'users/partners/verify.html')


@login_required
@require_POST
def resend(request):
    if not limited(request,'resend',5):
        svc.issue_verification(request.user)
        messages.success(request,'A verification link has been queued for your email.')
    else: messages.error(request,'Please wait before requesting another link.')
    return redirect('partner_setup')


@login_required
def setup(request):
    agent=TravelAgent.objects.filter(user=request.user).first()
    if not agent: return redirect('register_agent')
    profile=PartnerProfileForm(request.POST if request.method=='POST' and request.POST.get('action')=='profile' else None,instance=agent)
    affiliation=AffiliationForm(request.POST if request.method=='POST' and request.POST.get('action')=='affiliation' else None)
    if request.method=='POST':
        try:
            action=request.POST.get('action')
            if action=='profile' and profile.is_valid():
                profile.save();messages.success(request,'Your payment details and profile are saved.');return redirect('partner_setup')
            if action=='affiliation' and affiliation.is_valid():
                d=affiliation.cleaned_data
                svc.submit_claim(agent,d['agency_name'],d['agency_website'],d['payment_preference'],d['disclosure'],request.user)
                messages.success(request,'Your affiliation is recorded for confirmation.');return redirect('partner_setup')
            if action=='accept_payment':
                svc.accept_payment(request.user,request.POST.get('claim'));return redirect('partner_setup')
            if action=='independent':
                # Staff resolves outstanding disputed money; this does not silently clear any hold.
                claim=agent.affiliation_claims.filter(state__in=svc.OPEN_CLAIMS).last()
                if claim:
                    claim.state='rejected';claim.decision_reason='Agent requests independent status; staff review required.';claim.save()
                    svc.event(request.user,'independence_requested',claim)
                messages.info(request,'Grayson will review the affiliation correction. Booking remains available.')
        except (ValidationError,AffiliationClaim.DoesNotExist) as e: _error(request,e)
    return render(request,'users/partners/setup.html',dict(agent=agent,profile=profile,affiliation=affiliation,
        identity=PartnerIdentity.objects.filter(user=request.user).first(),claims=agent.affiliation_claims.order_by('-pk')[:10],
        applications=AgencyApplication.objects.filter(applicant=request.user),agencies=svc.managed_agencies(request.user),
        membership=AgencyMembership.objects.filter(user=request.user,active=True,booking_affiliation=True).select_related('agency').first()))


@login_required
def agency_workspace(request,pk=None,agency_id=None):
    agencies=svc.managed_agencies(request.user)
    selected=pk or agency_id or request.GET.get('agency')
    if not selected:
        if agencies.count()==1: return redirect('partner_agency',pk=agencies.first().pk)
        return render(request,'users/partners/agencies.html',{'agencies':agencies})
    agency=get_object_or_404(Agency,pk=selected)
    svc.require_manager(request.user,agency)
    form=AgencyProfileForm(request.POST if request.POST.get('action')=='profile' else None,agency=agency,
        initial={k:getattr(agency,k) for k in AgencyProfileForm.base_fields if hasattr(agency,k)})
    if request.method=='POST':
        try:
            action=request.POST.get('action')
            if action in ['add','reject']:
                if not getattr(settings,'PARTNER_MATCHING_ENABLED',False): raise ValidationError('Agency confirmation is temporarily paused.')
                claim=get_object_or_404(AffiliationClaim,pk=request.POST.get('claim'),agency=agency)
                svc.review_candidate(request.user,claim.pk,action,request.POST.get('payee',''),request.POST.get('reason',''))
            elif action in ['remove','role']:
                member=get_object_or_404(AgencyMembership,pk=request.POST.get('membership'),agency=agency)
                if action=='remove': svc.remove_member(request.user,member.pk,request.POST.get('reason',''))
                else: svc.change_role(request.user,member.pk,request.POST.get('role'),request.POST.get('reason',''))
            elif action=='profile' and form.is_valid():
                svc.require_manager(request.user,agency,owner=True)
                with transaction.atomic():
                    # Payout details go through the form helper so the account
                    # number is encrypted rather than set as a stray attribute.
                    for key,value in form.cleaned_data.items():
                        if key not in form.PAYOUT_FIELDS['bank']+form.PAYOUT_FIELDS['paypal']+form.PAYOUT_FIELDS['venmo']:
                            setattr(agency,key,value)
                    form.apply_payout_fields(agency)
                    agency.save()
                    svc.event(request.user,'agency_profile_updated',agency)
            else:
                if action!='profile': raise ValidationError('Unknown action.')
            if action!='profile' or form.is_valid():
                messages.success(request,'Agency updated.');return redirect('partner_agency',pk=agency.pk)
        except (ValidationError,ValueError) as e: _error(request,e)
    # Recheck verification when displaying; a changed email cannot remain visible.
    claims=agency.claims.filter(state='candidate').select_related('agent__user')
    candidates=[dict(id=c.pk,name=c.agent.agent_name,email=c.agent.user.email,affiliation=c.name,
        requested_payee=c.requested_payee) for c in claims if c.disclosed_at and c.disclosure_version and svc.email_verified(c.agent.user)]
    from reservations.models import Reservation
    rows=Reservation.objects.filter(partner_context__agency=agency).select_related('customer','travel_agent','partner_context').order_by('-created_at')
    member_filter=request.GET.get('agent')
    if member_filter: rows=rows.filter(travel_agent_id=member_filter)
    page=Paginator(rows,30).get_page(request.GET.get('page'))

    # Members: searchable and paged. An agency with 294 people cannot be a flat
    # list of expandable rows -- that was both unreadable and the reason this
    # page took the better part of a second to render.
    member_rows=agency.memberships.filter(active=True).select_related('user__travelagent')
    query=request.GET.get('q','').strip()
    if query:
        member_rows=member_rows.filter(Q(user__travelagent__agent_name__icontains=query)|
            Q(user__email__icontains=query)|Q(user__username__icontains=query))
    member_rows=member_rows.order_by('user__travelagent__agent_name','pk')
    member_count=agency.memberships.filter(active=True).count()
    money=TravelAgent.objects.filter(user_id__in=agency.memberships.filter(active=True).values('user_id')).aggregate(
        unpaid=Sum('unpaid_commissions'),pending=Sum('pending_commissions'),paid=Sum('total_paid_commission'))

    return render(request,'users/partners/agency.html',dict(agency=agency,agencies=agencies,candidates=candidates,
        members=Paginator(member_rows,25).get_page(request.GET.get('mpage')),
        member_count=member_count,member_query=query,
        member_shown=member_rows.count(),money=money,bookings=page,form=form,
        payouts=agency.commission_payouts.order_by('-paid_at')[:30],
        decisions=PartnerEvent.objects.filter(Q(object_type='AgencyMembership',object_id__in=agency.memberships.values('pk')) | Q(object_type='AffiliationClaim',object_id__in=agency.claims.values('pk'),kind__in=['candidate_add','candidate_reject','payment_accepted'])).order_by('-pk')[:30],
        can_own=request.user.is_staff or agency.memberships.filter(user=request.user,active=True,role='owner').exists(),
        # Staff may open any agency to support it. Say so on the page, so an
        # owner action is never taken while believing you are somewhere else.
        viewing_as_staff=request.user.is_staff and not agency.memberships.filter(
            user=request.user,active=True,role__in=['owner','admin']).exists(),
        matching_enabled=getattr(settings,'PARTNER_MATCHING_ENABLED',False)))


@login_required
def agent_detail(request,pk):
    agent=get_object_or_404(TravelAgent,pk=pk)
    if agent.user_id==request.user.pk or request.user.is_staff:
        from .views import AgentDetailView
        return AgentDetailView.as_view()(request,pk=pk)
    agencies=svc.managed_agencies(request.user).filter(Q(booking_contexts__agent=agent)|Q(memberships__user=agent.user)).distinct()
    selected=request.GET.get('agency')
    if selected: agencies=agencies.filter(pk=selected)
    if not agencies.exists(): raise Http404
    if agencies.count()>1: return render(request,'users/partners/agencies.html',{'agencies':agencies})
    return redirect(reverse('partner_agency',kwargs={'pk':agencies.first().pk})+'?agent='+str(agent.pk))


@staff_member_required
def staff_workspace(request):
    preview=None
    if request.method=='POST':
        try:
            action=request.POST.get('action');reason=request.POST.get('reason','')
            if action=='application': svc.review_application(request.user,int(request.POST['application']),request.POST['decision'],reason,request.POST.get('agency') or None)
            elif action=='alias':
                svc.require_staff(request.user,reason)
                alias=AgencyAlias(agency_id=request.POST['agency'],name=request.POST['name']);alias.full_clean();alias.save()
                svc.event(request.user,'alias_added',alias,reason=reason);svc.rematch_claims()
            elif action=='match':
                svc.require_staff(request.user,reason)
                with transaction.atomic():
                    claim=AffiliationClaim.objects.select_for_update().get(pk=request.POST['claim'])
                    if claim.state not in ['unmatched','ambiguous']: raise ValidationError('This claim is no longer awaiting a match.')
                    claim.agency=get_object_or_404(Agency,pk=request.POST['agency'],is_active=True)
                    if not svc.email_verified(claim.agent.user) or not claim.disclosed_at: raise ValidationError('Verify identity and disclosure before showing a candidate.')
                    claim.state='candidate';claim.save();svc.event(request.user,'claim_matched',claim,reason=reason)
            elif action=='independent':
                agent=get_object_or_404(TravelAgent,pk=request.POST['agent'])
                svc.confirm_independent(request.user,agent,reason)
            elif action=='assign':
                agency=get_object_or_404(Agency,pk=request.POST['agency'],is_active=True)
                ids=[int(s.strip()) for s in request.POST['agents'].split(',') if s.strip()]
                if not ids or len(ids)>500: raise ValidationError('Provide 1–500 agent IDs.')
                with transaction.atomic():
                    agents=list(TravelAgent.objects.filter(pk__in=ids).order_by('pk'))
                    if len(agents)!=len(set(ids)): raise ValidationError('One or more agents were not found.')
                    for agent in agents: svc.assign_member(request.user,agent,agency,request.POST['payee'],reason,role=request.POST.get('role','agent'))
            elif action=='backfill':
                ids=[int(s.strip()) for s in request.POST.get('bookings','').split(',') if s.strip()]
                if not ids:
                    from reservations.models import Reservation
                    agents=[int(s.strip()) for s in request.POST.get('backfill_agents','').split(',') if s.strip()]
                    start=date.fromisoformat(request.POST.get('date_from',''))
                    end=date.fromisoformat(request.POST.get('date_to',''))
                    if not agents or start>end: raise ValidationError('Select agents and a valid date range, or provide booking IDs.')
                    ids=list(Reservation.objects.filter(travel_agent_id__in=agents,legs__pickup_date__range=(start,end)).values_list('pk',flat=True).distinct()[:501])
                agency=get_object_or_404(Agency,pk=request.POST['agency']) if request.POST.get('agency') else None
                preview=svc.preview_backfill(request.user,ids,agency,'visibility' in request.POST,'payment' in request.POST,request.POST.get('payee','direct'),reason)
            elif action=='apply_backfill': svc.apply_backfill(request.user,request.POST['preview'])
            elif action=='adjustment':
                svc.record_payout_adjustment(request.user,int(request.POST['payout']),Decimal(request.POST['amount']),request.POST['reference'],reason)
            elif action=='retry':
                svc.require_staff(request.user,reason)
                out=get_object_or_404(PartnerOutbox,pk=request.POST['outbox'],state='failed')
                out.state='pending';out.available_at=timezone.now();out.attempts=0;out.save()
                svc.event(request.user,'outbox_retried',out,reason=reason)
            else: raise ValidationError('Unknown staff action.')
            if not preview: messages.success(request,'Partner decision saved.');return redirect('partner_staff')
        except (ValidationError,ValueError,KeyError,InvalidOperation,CommissionPayout.DoesNotExist,AgencyApplication.DoesNotExist,AffiliationClaim.DoesNotExist) as e: _error(request,e)
    counts=dict(agency_applications=AgencyApplication.objects.filter(state__in=['pending','information']).count(),
        held_bookings=PartnerBooking.objects.exclude(hold='').filter(reservation__commission_paid=False).count(),
        failed_emails=PartnerOutbox.objects.filter(state='failed').count())
    preview_total=sum((Decimal(row['commission']) for row in preview.rows),Decimal('0')) if preview else None
    return render(request,'users/partners/staff.html',dict(preview=preview,preview_total=preview_total,counts=counts,
        applications=AgencyApplication.objects.select_related('applicant').exclude(state='approved').order_by('created_at')[:100],
        claims=AffiliationClaim.objects.select_related('agent__user','agency').exclude(state__in=['approved','superseded']).order_by('created_at')[:150],
        agencies=Agency.objects.filter(is_active=True).order_by('name'),
        # Staff pick people by name, not by remembering a numeric id.
        agent_choices=TravelAgent.objects.select_related('user').order_by('agent_name','pk'),
        failures=PartnerOutbox.objects.filter(state='failed').order_by('created_at')[:50],
        adjustments=PartnerPayoutAdjustment.objects.select_related('payout__agent','payout__agency').order_by('-pk')[:50],
        events=PartnerEvent.objects.select_related('actor').order_by('-pk')[:50],
        headless=Agency.objects.filter(is_active=True).exclude(memberships__active=True,memberships__role='owner'),
        duplicate_emails=User.objects.extra(select={'normalized':'LOWER(email)'}).values('normalized').annotate(total=Count('pk')).filter(total__gt=1).exclude(email=''),
        unlinked=TravelAgent.objects.filter(agency=None).exclude(agency_name__in=['',' ']).count()))


@staff_member_required
def payout_board(request):
    from users.eligibility import get_commission_eligibility
    from reservations.models import Reservation
    rows=Reservation.objects.filter(travel_agent__isnull=False,commission_paid=False).select_related(
        'partner_context__agency','partner_context__payee_agency','partner_context__agent__user','travel_agent__user','customer').prefetch_related('legs')
    groups={};held=[]
    for r in rows.iterator(chunk_size=200):
        result=get_commission_eligibility(r)
        if result.needs_review:
            held.append(dict(id=r.pk,agent=r.travel_agent.agent_name,reason=result.reason,amount=result.commission))
        if not result.safe_to_pay: continue
        c=r.partner_context
        kind='agency' if c.payee_agency_id else 'agent';pk=c.payee_agency_id or r.travel_agent_id
        key=(kind,pk)
        if key not in groups: groups[key]=dict(kind=kind,id=pk,name=c.payee_agency.name if c.payee_agency_id else r.travel_agent.agent_name,total=Decimal('0'),count=0)
        groups[key]['total']+=result.commission;groups[key]['count']+=1
    return render(request,'users/partners/payouts.html',{'groups':groups.values(),'held':held})


@require_POST
def save_draft(request):
    if limited(request,'draft',120): return JsonResponse({'error':'Please slow down.'},status=429)
    # Account numbers are never parked in a session, only in the encrypted field.
    allowed=set(PartnerRegistrationForm.base_fields)-{'password1','password2','terms','disclosure','bank_account_number'}
    request.session['partner_draft']={key:value[:2000] for key,value in request.POST.items() if key in allowed}
    return JsonResponse({'saved':True})
