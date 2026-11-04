"""Pseudonymous login counters for isolated, fictional-data tests only.

This is not a complete production anti-abuse system. It deliberately uses
REMOTE_ADDR and never trusts forwarded headers; a proxy requires a separately
reviewed trust policy before any real deployment.
"""
from datetime import datetime, timedelta, timezone as dt_timezone
import ipaddress

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import LocalLoginThrottleBucket

WINDOW_SECONDS = 600
SOURCE_LIMIT = 30
PAIR_LIMIT = 5


def _bucket(scope, value, start, end):
    digest = salted_hmac(
        "ips.local_login_throttle." + scope, value,
        secret=settings.IPS_LOCAL_LOGIN_THROTTLE_KEY, algorithm="sha256",
    ).hexdigest()
    bucket, _ = LocalLoginThrottleBucket.objects.select_for_update().get_or_create(
        scope=scope, digest=digest, window_start=start,
        defaults={"expires_at": end},
    )
    return bucket


def consume_budget(remote_addr, username, *, now=None):
    """Reserve one request slot; invalid sources and a full budget fail closed."""
    try:
        address = str(ipaddress.ip_address(remote_addr))
    except (TypeError, ValueError):
        return False, WINDOW_SECONDS
    if not isinstance(username, str):
        username = ""
    # Bound attacker-controlled input without persisting names or addresses.
    username = username[:150]
    now = now or timezone.now()
    timestamp = int(now.timestamp())
    start_seconds = timestamp - timestamp % WINDOW_SECONDS
    start = datetime.fromtimestamp(start_seconds, tz=dt_timezone.utc)
    end = start + timedelta(seconds=WINDOW_SECONDS)
    retry = max(1, int((end - now).total_seconds()) + 1)
    with transaction.atomic():
        source = _bucket("source", address, start, end)
        if source.attempts >= SOURCE_LIMIT:
            return False, retry
        source.attempts += 1
        source.save(update_fields=["attempts"])

        pair = _bucket("pair", address + "\0" + username, start, end)
        if pair.attempts >= PAIR_LIMIT:
            return False, retry
        pair.attempts += 1
        pair.save(update_fields=["attempts"])
    return True, retry
