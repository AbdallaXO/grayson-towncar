from django.shortcuts import render


# Create your views here.
def index(request):
    return render(request, "services/index.html")


def orlando_airport_transportation(request):
    return render(request, "services/orlando-airport-transportation.html")


def disney_world_transportation(request):
    return render(request, "services/disney-world-transportation.html")


def universal_orlando_transportation(request):
    return render(request, "services/universal-orlando-transportation.html")


def port_canaveral_transportation(request):
    return render(request, "services/port-canaveral-transportation.html")


def corporate_transportation(request):
    return render(request, "services/corporate-transportation.html")
