from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from .models import TravelAgent, Agency
from .payout_forms import PayoutDetailsMixin


class PartnerRegistrationForm(PayoutDetailsMixin, forms.Form):
    account_type = forms.ChoiceField(label='How will you use Grayson?', choices=[('agent','Register as a travel agent'),('agency','Register my agency')], widget=forms.RadioSelect)
    agent_name = forms.CharField(label='Your full name',max_length=100)
    email = forms.EmailField()
    phone = forms.CharField(max_length=20)
    username = forms.CharField(max_length=150)
    password1 = forms.CharField(label='Password',widget=forms.PasswordInput)
    password2 = forms.CharField(label='Confirm password',widget=forms.PasswordInput)
    agency_name = forms.CharField(label='Agency name (optional for travel agents)',max_length=100,required=False)
    agency_website = forms.URLField(label='Agency website',required=False)
    address = forms.CharField(label='Agency address',required=False,widget=forms.Textarea(attrs={'rows':2}))
    authority = forms.CharField(label='For agency owners: your role and authority to represent this business',required=False,widget=forms.Textarea(attrs={'rows':2}))
    payment_preference = forms.ChoiceField(label='Directly, or my agency?',
        help_text='Choose your agency and we pay them, and they pay you. If we do not have your agency on file yet, ask them to register with us — or to send us their payment details — before commission can be released.',
        choices=[('direct','Directly'),('agency','My agency')],widget=forms.RadioSelect)
    # Shown only when they choose to be paid directly; the agency-paid majority
    # never sees these, and anyone can leave them for later.
    payment_method = forms.ChoiceField(label='Payment method',required=False,choices=[])
    disclosure = forms.BooleanField(required=False,label='If I name an agency, share my name, email and how I want to be paid with them, so they can confirm I work there. My bookings and earnings stay private.')
    terms = forms.BooleanField(label='I accept the partner terms below.')

    def __init__(self,*args,user=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.user=user if user and user.is_authenticated else None
        self.fields['payment_method'].choices=[('','Choose how you want to be paid')]+[
            (k,dict(TravelAgent.PAYMENT_METHOD_CHOICES).get(k,k)) for k in TravelAgent.SELECTABLE_PAYMENT_METHODS]
        self.add_payout_fields()
        # Placeholders per the registration design handoff: an example beats a
        # label for formats people get wrong.
        for name, placeholder in {
            'username':'Choose a username','email':'you@youragency.com',
            'password1':'At least 8 characters','password2':'Repeat your password',
            'agent_name':'First and last name','agency_name':'Your agency or host agency',
            'phone':'(407) 555-0100','agency_website':'https://youragency.com',
            'paypal_email':'you@youragency.com','venmo_handle':'JaneWhitfield',
            'bank_account_name':'Name exactly as it appears on the account',
            'bank_routing_number':'9 digits','bank_account_number':'Your account number',
        }.items():
            if name in self.fields:
                self.fields[name].widget.attrs.setdefault('placeholder', placeholder)
        # Dynamically added fields render last by default, which would put the
        # payout boxes below the terms checkbox, detached from the method that
        # reveals them. Keep them with their section.
        self.order_fields(['account_type','agent_name','email','phone','username','password1','password2',
            'agency_name','agency_website','address','authority','disclosure',
            'payment_preference','payment_method','paypal_email','venmo_handle',
            'bank_account_name','bank_routing_number','bank_account_type','bank_account_number',
            'terms'])
        if self.user:
            for name in ['username','password1','password2']:
                self.fields.pop(name)
            self.fields['email'].disabled=True
            self.initial['email']=self.user.email

    def clean_email(self):
        email=self.cleaned_data['email'].strip().lower()
        others=User.objects.filter(email__iexact=email)
        if self.user:
            others=others.exclude(pk=self.user.pk)
        if others.exists():
            raise ValidationError('An account already uses this email. Sign in to reuse it, or contact Grayson if it is shared.')
        return email

    def clean_username(self):
        name=self.cleaned_data['username'].strip().lower()
        User._meta.get_field('username').run_validators(name)
        if User.objects.filter(username__iexact=name).exists():
            raise ValidationError('This username is already in use. Sign in or choose another.')
        return name

    def clean(self):
        data=super().clean()
        if not self.user:
            if data.get('password1') != data.get('password2'):
                self.add_error('password2','Passwords do not match.')
            if data.get('password1'):
                try:
                    validate_password(data['password1'],User(username=data.get('username',''),email=data.get('email','')))
                except ValidationError as e:
                    self.add_error('password1',e)
        agency=(data.get('agency_name') or '').strip()
        if agency and not data.get('disclosure'):
            self.add_error('disclosure','Please confirm the agency information-sharing disclosure.')
        if data.get('account_type')=='agency':
            if not agency: self.add_error('agency_name','Enter your agency name.')
            if not data.get('authority'): self.add_error('authority','Explain your authority to register this agency.')
            if data.get('payment_preference')!='agency':
                self.add_error('payment_preference','Agency registration starts with agency payment. Individual payment exceptions can be reviewed later.')
        elif not agency and data.get('payment_preference')=='agency':
            self.add_error('payment_preference','Name your agency or choose direct payment.')
        # Payout details are optional at signup, but a half-filled rail is worse
        # than none -- it looks set up and pays nobody.
        if data.get('payment_preference')=='direct' and data.get('payment_method'):
            self.validate_selected_rail(data)
        elif data.get('payment_preference')!='direct':
            # Agency-paid partners never fill these in; discard any stray values.
            for name in ['payment_method']+sum(self.PAYOUT_FIELDS.values(),[]):
                data[name]=''
        return data

    def payout_chosen(self):
        """True when the applicant supplied their own payout details."""
        return bool(self.cleaned_data.get('payment_method'))


def selectable_methods(current=''):
    """Rails a partner may choose, plus whichever retired rail they already use.

    Retired rails are never offered to someone new, but an existing agent on one
    must not have it silently swapped when they edit their name.
    """
    labels=dict(TravelAgent.PAYMENT_METHOD_CHOICES)
    keys=list(TravelAgent.SELECTABLE_PAYMENT_METHODS)
    if current and current not in keys and current!='agency':
        keys.append(current)
    return [('', 'Complete later')]+[(k,f'{labels.get(k,k)} (no longer offered)' if k in TravelAgent.RETIRED_PAYMENT_METHODS else labels.get(k,k)) for k in keys]


class PartnerProfileForm(PayoutDetailsMixin, forms.ModelForm):
    class Meta:
        model=TravelAgent
        fields=['agent_name','phone','payment_method']
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['payment_method'].choices=selectable_methods(getattr(self.instance,'payment_method','') or '')
        self.fields['payment_method'].help_text='Your own payout details. Agency payment routing is reviewed separately.'
        self.add_payout_fields(self.instance if self.instance and self.instance.pk else None)
    def clean(self):
        data=super().clean()
        return self.validate_selected_rail(data, self.instance if self.instance and self.instance.pk else None)
    def save(self,commit=True):
        obj=super().save(commit=False)
        self.apply_payout_fields(obj)
        if commit: obj.save()
        return obj


class AffiliationForm(forms.Form):
    agency_name=forms.CharField(max_length=100)
    agency_website=forms.URLField(required=False)
    payment_preference=forms.ChoiceField(label='Directly, or my agency?',choices=[('direct','Directly'),('agency','My agency')])
    disclosure=forms.BooleanField(label='Share my name, email and how I want to be paid with this agency, so they can confirm I work there. My bookings and earnings stay private.')


class AgencyProfileForm(PayoutDetailsMixin, forms.Form):
    phone=forms.CharField(max_length=20,required=False)
    address=forms.CharField(required=False,widget=forms.Textarea(attrs={'rows':2}))
    website=forms.URLField(required=False)
    payout_policy=forms.ChoiceField(label='Who does Grayson pay for your agents?',
        help_text='Choose the second option if your agency pays its own agents. Agents can then never arrange direct payment with Grayson.',
        choices=Agency.PAYOUT_POLICY_CHOICES,widget=forms.RadioSelect,required=False)
    payment_method=forms.ChoiceField(choices=[],required=False)
    def __init__(self,*args,agency=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.agency=agency
        self.fields['payment_method'].choices=selectable_methods(getattr(agency,'payment_method','') or '')
        self.add_payout_fields(agency)
    def clean(self):
        data=super().clean()
        return self.validate_selected_rail(data, self.agency)
