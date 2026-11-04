"""Local development settings. This configuration is deliberately not production-ready."""
import os
import secrets
import tempfile

from django.core.exceptions import ImproperlyConfigured

from .settings_common import *  # noqa: F403

RUNTIME_DIR = BASE_DIR / ".runtime"
RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)

# A new local key is generated at each start; local sessions end on restart.
SECRET_KEY = secrets.token_urlsafe(48)


def _review_ai_fingerprint_key():
    """Keep consent fingerprints stable while local login sessions rotate."""
    configured = os.environ.get("IPS_REVIEW_AI_FINGERPRINT_KEY", "")
    if configured:
        if len(configured) < 32:
            raise ImproperlyConfigured("IPS_REVIEW_AI_FINGERPRINT_KEY must contain at least 32 characters.")
        return configured
    path = RUNTIME_DIR / "review-ai-fingerprint.key"
    if not path.exists():
        fd, temporary = tempfile.mkstemp(prefix="review-ai-key-", dir=RUNTIME_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(secrets.token_urlsafe(48))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass  # Another process installed the key first.
        finally:
            os.unlink(temporary)
    key = path.read_text(encoding="utf-8")
    if len(key) < 32:
        raise ImproperlyConfigured("The local review AI fingerprint key is invalid.")
    return key


IPS_REVIEW_AI_FINGERPRINT_KEY = _review_ai_fingerprint_key()
DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver", "[::1]"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": RUNTIME_DIR / "dev.sqlite3"}}
MEDIA_ROOT = RUNTIME_DIR / "private-media"
# Deliberately do not expose MEDIA_ROOT through any URL or static-file route.

# Local development only. Provider calls also require the applicable journal or
# publisher policy and role, metadata consent where applicable, and confirmation.
IPS_OPENAI_ENABLED = os.environ.get("IPS_OPENAI_ENABLED") == "1"
IPS_OPENAI_MODEL = os.environ.get("IPS_OPENAI_MODEL", "").strip()
IPS_OPENAI_IMAGE_ENABLED = os.environ.get("IPS_OPENAI_IMAGE_ENABLED") == "1"
IPS_OPENAI_IMAGE_MODEL = os.environ.get("IPS_OPENAI_IMAGE_MODEL", "").strip()

# Only for locally bound, fictional-data publishing demonstrations.
IPS_LOCAL_PUBLICATION_ENABLED = DEBUG and os.environ.get("IPS_LOCAL_PUBLICATION_ENABLED") == "1"
