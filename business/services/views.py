from django.shortcuts import render

# Create your views here.d
def index(request):
    return render(request, 'services/index.html')