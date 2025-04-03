from django.shortcuts import render
from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from .forms import CustomUserCreationForm


def index(request):
    return render(request, "users/profiles.html")


def registerUser(request):
    form = CustomUserCreationForm()
    page = 'register'
    context = {'page':page, 'form':form}
    if request.method == 'POST':
        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.username = user.username.lower()
            user.save()
            messages.success(request, f'Hello {user.username} Your account was created successfully!', extra_tags='success')
            
            login(request, user)
            return redirect('home')
        else:
            messages.error(request, "An Error has Occoured during registration.", extra_tags='danger')
    return render(request, 'users/login_register.html', context)


def loginUser(request):
    page = 'login'   
    if request.user.is_authenticated:
        return redirect("home")
    if request.method == "POST":
        username = request.POST["username"]
        password = request.POST["password"]
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            messages.success(request, "Successfully logged in", extra_tags="success")
            return redirect("home")
        else:
            messages.error(
                request, "Please Enter Valid Credentials", extra_tags="danger"
            )

    return render(request, "users/login_register.html", {'page':page})


@login_required(login_url="login")
def logoutUser(request):
    """
    Simply Logs out the user once they click logout
    """
    logout(request)
    messages.success(request, "You Have Been Logged Out!")
    return redirect("login")

def member(request):
    return render(request, "users/become_member.html")