"""
Middleware to store request in thread-local storage for audit logging.
This allows signals to access the current request and user.
"""
from threading import local
import time
import logging

from django.conf import settings
from django.db import connection

_perf_logger = logging.getLogger('perf')

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


class SlowRequestMiddleware:
    """Logs any request that takes longer than 500ms."""

    THRESHOLD_MS = 500

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # connection.queries only accumulates under DEBUG; snapshot the count
        # before the view so we report queries for THIS request, not the worker total.
        queries_before = len(connection.queries) if settings.DEBUG else 0
        t0 = time.perf_counter()
        response = self.get_response(request)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if elapsed_ms > self.THRESHOLD_MS:
            if settings.DEBUG:
                query_count = len(connection.queries) - queries_before
                _perf_logger.warning(
                    "SLOW %s %s — %.0fms, %d queries",
                    request.method,
                    request.get_full_path(),
                    elapsed_ms,
                    query_count,
                )
            else:
                _perf_logger.warning(
                    "SLOW %s %s — %.0fms",
                    request.method,
                    request.get_full_path(),
                    elapsed_ms,
                )
        return response


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
