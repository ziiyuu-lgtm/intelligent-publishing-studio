"""Blocked release-candidate configuration. Not suitable for real data.

This profile prepares settings for future integration work. It deliberately
blocks every HTTP request and fails ``check --deploy`` until the publishing
workflow and its operational protections have been independently completed.
"""
import os
import re
import stat
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from .settings_common import *  # noqa: F403


def _required(name):
    value = os.environ.get(name, "")
    if not value or value != value.strip() or any(char in value for char in "\r\n\0"):
        raise ImproperlyConfigured(f"{name} must be set to a non-empty single-line value.")
    return value


def _key(name):
    value = _required(name)
    if len(value) < 50 or len(set(value)) < 5 or value.startswith("django-insecure-"):
        raise ImproperlyConfigured(f"{name} must be a strong, randomly generated key of at least 50 characters.")
    return value


def _private_root():
    raw = Path(_required("IPS_PRIVATE_DATA_ROOT"))
    if not raw.is_absolute() or raw.is_symlink():
        raise ImproperlyConfigured("IPS_PRIVATE_DATA_ROOT must be an absolute, non-symlink directory.")
    try:
        root = raw.resolve(strict=True)
        info = root.stat()
    except OSError as exc:
        raise ImproperlyConfigured("IPS_PRIVATE_DATA_ROOT must be an existing private directory.") from exc
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISDIR(info.st_mode) or mode & 0o077 or mode & 0o700 != 0o700:
        raise ImproperlyConfigured("IPS_PRIVATE_DATA_ROOT must be a private owner-readable/writable directory (0700).")
    if root.is_relative_to(BASE_DIR.parent.resolve()):
        raise ImproperlyConfigured("IPS_PRIVATE_DATA_ROOT must be outside the source checkout.")
    return root


def _hosts():
    values = [value.strip().lower() for value in _required("IPS_ALLOWED_HOSTS").split(",")]
    label = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
    for value in values:
        if not value or len(value) > 253 or not all(label.fullmatch(part) for part in value.split(".")):
            raise ImproperlyConfigured("IPS_ALLOWED_HOSTS must contain exact hostnames without ports or wildcards.")
    return list(dict.fromkeys(values))


def _db_identifier(name):
    value = _required(name)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,62}", value):
        raise ImproperlyConfigured(f"{name} must be a simple database identifier.")
    return value


SECRET_KEY = _key("DJANGO_SECRET_KEY")
IPS_REVIEW_AI_FINGERPRINT_KEY = _key("IPS_REVIEW_AI_FINGERPRINT_KEY")
if SECRET_KEY == IPS_REVIEW_AI_FINGERPRINT_KEY:
    raise ImproperlyConfigured("The Django and review fingerprint keys must be different.")

RUNTIME_DIR = _private_root()
MEDIA_ROOT = RUNTIME_DIR / "private-media"
STATIC_ROOT = RUNTIME_DIR / "collected-static"

DEBUG = False
ALLOWED_HOSTS = _hosts()
CSRF_TRUSTED_ORIGINS = [f"https://{host}" for host in ALLOWED_HOSTS]

_db_port = _required("IPS_DB_PORT")
if not _db_port.isascii() or not _db_port.isdecimal() or not 1 <= int(_db_port) <= 65535:
    raise ImproperlyConfigured("IPS_DB_PORT must be a valid TCP port.")
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": _db_identifier("IPS_DB_NAME"),
    "USER": _db_identifier("IPS_DB_USER"),
    "PASSWORD": _required("IPS_DB_PASSWORD"),
    # Candidate profile intentionally restricts the database to this machine.
    "HOST": "127.0.0.1",
    "PORT": _db_port,
    "CONN_MAX_AGE": 0,
}}

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 60
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SECURE_REFERRER_POLICY = "same-origin"
# No proxy header is trusted in the blocked candidate. A later deployment must
# explicitly configure and verify a proxy that strips untrusted client headers.
SECURE_PROXY_SSL_HEADER = None

EMAIL_BACKEND = "config.unavailable_email.EmailBackend"
IPS_OPENAI_ENABLED = False
IPS_OPENAI_MODEL = ""
IPS_OPENAI_IMAGE_ENABLED = False
IPS_OPENAI_IMAGE_MODEL = ""
IPS_LOCAL_PUBLICATION_ENABLED = False
IPS_LOCAL_LOGIN_THROTTLE_ENABLED = False
IPS_LOCAL_LOGIN_THROTTLE_KEY = ""
IPS_LOCAL_REVIEW_MAIL_ENABLED = False
IPS_LOCAL_REVIEW_MAIL_KEY = ""
IPS_LOCAL_REGISTRATION_ENABLED = False
IPS_LOCAL_REGISTRATION_KEY = ""

# This is intentionally not controlled by an environment variable. Removing
# the block requires a separate reviewed code change after release criteria.
IPS_RELEASE_CANDIDATE_BLOCKED = True
MIDDLEWARE = ["publishing.release_guard.BlockAllRequestsMiddleware", *MIDDLEWARE]
