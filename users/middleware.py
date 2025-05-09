from django.core.cache import cache
from django.http import HttpResponse
from datetime import datetime, timedelta
from .models import NewsletterSubscriptionAttempt

class NewsletterRateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        # Maximum number of attempts allowed per hour
        self.max_attempts = 5
        # Time window in seconds (1 hour)
        self.time_window = 3600

    def __call__(self, request):
        if request.path == '/newsletter/subscribe/' and request.method == 'POST':
            ip_address = self.get_client_ip(request)
            email = request.POST.get('email')
            
            if not self.is_allowed(ip_address, email):
                return HttpResponse('Too many subscription attempts. Please try again later.', status=429)
        
        return self.get_response(request)
    
    def get_client_ip(self, request):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip
    
    def is_allowed(self, ip_address, email):
        # Check recent attempts from this IP
        recent_attempts = NewsletterSubscriptionAttempt.objects.filter(
            ip_address=ip_address,
            timestamp__gte=datetime.now() - timedelta(seconds=self.time_window)
        ).count()
        
        if recent_attempts >= self.max_attempts:
            return False
        
        # Check if this email was recently attempted
        recent_email_attempts = NewsletterSubscriptionAttempt.objects.filter(
            email=email,
            timestamp__gte=datetime.now() - timedelta(seconds=self.time_window)
        ).count()
        
        if recent_email_attempts >= self.max_attempts:
            return False
        
        return True 