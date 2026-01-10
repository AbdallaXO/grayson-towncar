"""
Middleware to store request in thread-local storage for audit logging.
This allows signals to access the current request and user.
"""
from threading import local

_thread_locals = local()


def get_current_request():
    """Get the current request from thread-local storage"""
    return getattr(_thread_locals, 'request', None)


def get_current_user():
    """Get the current user from thread-local storage"""
    request = get_current_request()
    if request and hasattr(request, 'user'):
        return request.user
    return None


class ThreadLocalMiddleware:
    """
    Middleware that stores the current request in thread-local storage.
    This allows signals and other code to access the request and user.
    """
    
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request):
        # Store request in thread-local storage
        _thread_locals.request = request
        
        try:
            response = self.get_response(request)
        finally:
            # Clean up thread-local storage
            if hasattr(_thread_locals, 'request'):
                delattr(_thread_locals, 'request')
        
        return response
