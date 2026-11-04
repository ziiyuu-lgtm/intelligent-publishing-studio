"""Optional test-only sign-in throttling; the normal login remains unchanged."""
from django.conf import settings
from django.contrib.auth.views import LoginView
from django.http import HttpResponse
from django.utils.translation import gettext as _

from .local_login_throttle import consume_budget


class LocalThrottledLoginView(LoginView):
    def post(self, request, *args, **kwargs):
        if settings.DEBUG and getattr(settings, "IPS_LOCAL_LOGIN_THROTTLE_ENABLED", False):
            allowed, retry = consume_budget(request.META.get("REMOTE_ADDR"), request.POST.get("username", ""))
            if not allowed:
                response = HttpResponse(_("Too many sign-in attempts. Try again later."), status=429)
                response["Retry-After"] = str(retry)
                return response
        return super().post(request, *args, **kwargs)
