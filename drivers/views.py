from django.shortcuts import get_object_or_404
from django.http import HttpResponse
from .models import Driver


# Create your views here.
def index(request):
    driver = get_object_or_404(Driver, profile=request.user)
    return HttpResponse("Hello")
