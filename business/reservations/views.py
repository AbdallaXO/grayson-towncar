from django.shortcuts import render


# Create your views here.
def index(request):
    return render(request, "reservations/index.html")


def faqs(request):
    return render(request, 'reservations/faqs.html')