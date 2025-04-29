from django.shortcuts import get_object_or_404
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
    driver = get_object_or_404(Driver, profile=request.user)
    selected_date = request.GET.get("date")
    try:
        if selected_date:
            selected_date = datetime.strptime(selected_date, "%Y-%m-%d").date()
        else:
            selected_date = timezone.localdate()
    except ValueError:
        selected_date = timezone.localdate()

    legs = Leg.objects.filter(driver=driver, pickup_date=selected_date)

    return render(
        request, "drivers/index.html", {"legs": legs, "selected_date": selected_date}
    )


def all_legs(request):
    driver = get_object_or_404(Driver, profile=request.user)
    legs = Leg.objects.filter(driver=driver)
    return render(request, "drivers/all_legs.html", {"legs":legs})

def week_schedule(request):
    driver = get_object_or_404(Driver, profile=request.user)
    today = timezone.localdate()
    next_week = today + timezone.timedelta(days=7)
    legs = Leg.objects.filter(driver=driver, pickup_date__gte=today, pickup_date__lte=next_week).order_by('pickup_date', 'pickup_time')
    return render(request, "drivers/weekly_schedule.html", {
        "legs": legs,
        "today": today,
        "next_week": next_week
    })



