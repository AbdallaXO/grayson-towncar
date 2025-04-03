from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django import forms

class CustomUserCreationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ['username', 'email', 'password1', 'password2']
        
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields:
            self.fields[field].widget.attrs.update({
                'class': 'input',
            })
        # Clean labels
        self.fields['password1'].label = 'Password'
        self.fields['password2'].label = 'Confirm Password'

        # Remove djangos default help text clutter with that form
        self.fields['username'].help_text = ''
        self.fields['password1'].help_text = ''
        self.fields['password2'].help_text = ''

       
        self.fields['email'].widget.attrs.update({'type': 'email'})
        self.fields['email'].required = True

        placeholders = {
            'username': 'Enter username',
            'email': 'Enter email address',
            'password1': 'Create a password',
            'password2': 'Confirm your password',
        }
        for field, placeholder in placeholders.items():
            self.fields[field].widget.attrs.update({'placeholder': placeholder})
