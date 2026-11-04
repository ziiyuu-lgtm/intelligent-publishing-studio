"""Block all request traffic in the intentionally incomplete candidate."""
from django.http import HttpResponse


class BlockAllRequestsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = HttpResponse("Service unavailable.\n", status=503, content_type="text/plain; charset=utf-8")
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
