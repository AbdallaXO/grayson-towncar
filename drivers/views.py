from django.shortcuts import get_object_or_404
from django.http import HttpResponse
from django.shortcuts import render
from .models import Driver
from datetime import datetime
from reservations.models import Leg
from django.contrib.auth.decorators import login_required
from django.utils import timezone


@login_required(login_url="login")
def index(request):
    """
    Driver Dashboard Shows All Legs - Lets you Filter by Date
    """
    # Get the logged-in driver
    driver = get_object_or_404(Driver, profile=request.user)
    
    # Get date from query parameters or use today
    selected_date = request.GET.get("date")
    try:
        if selected_date:
            selected_date = datetime.strptime(selected_date, "%Y-%m-%d").date()
        else:
            selected_date = timezone.localdate()
    except ValueError:
        selected_date = timezone.localdate()
    
    legs = Leg.objects.filter(driver=driver, pickup_date=selected_date)
    
    return render(request, 'drivers/index.html', {
        'legs': legs,
        'selected_date': selected_date
    })
