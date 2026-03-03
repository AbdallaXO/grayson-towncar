import time
import logging

logger = logging.getLogger('perf')

SLOW_REQUEST_THRESHOLD_MS = 500


class SlowRequestMiddleware:
    """
    Logs any request that takes longer than SLOW_REQUEST_THRESHOLD_MS milliseconds.
    Works in both DEBUG and production modes (uses perf_counter, not DB query count).
    Add after SessionMiddleware in settings.MIDDLEWARE.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        t0 = time.perf_counter()
        response = self.get_response(request)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if elapsed_ms > SLOW_REQUEST_THRESHOLD_MS:
            logger.warning(
                "SLOW %s %s — %.0fms",
                request.method,
                request.get_full_path(),
                elapsed_ms,
            )
        return response
