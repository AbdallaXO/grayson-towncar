from django.shortcuts import render
from . models import UserProfile
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.shortcuts import redirect
from django.contrib import messages
# Create your views here.
def index(request):
    return render(request, 'users/profiles.html')

def loginUser(request):
    if request.user.is_authenticated:
        return redirect('home')
    if request.method == 'POST':
        username = request.POST['username']
        password = request.POST['password']
        print(username, password)
        user = authenticate(request, username=username, password=password)
        print(user)
        if user is not None:
            login(request, user)
            messages.success(request, 'Successfully logged in')
            return redirect('home')
    return render(request, 'users/login_register.html')
def logoutUser(request):
    """
    Simply Logs out the user once they click logout
    """
    logout(request)
    return redirect('login')
        
    