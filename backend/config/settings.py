"""Local development settings. This configuration is deliberately not production-ready."""
from pathlib import Path
import os
import secrets
from django.utils.translation import gettext_lazy as _

BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = BASE_DIR / ".runtime"
RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)

# A new local key is generated at each start; local sessions end on restart.
SECRET_KEY = secrets.token_urlsafe(48)
DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver", "[::1]"]
INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "publishing.apps.PublishingConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": RUNTIME_DIR / "dev.sqlite3"}}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "en"
LANGUAGES = [("en", _("English")), ("zh-hans", _("Simplified Chinese"))]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_ROOT = RUNTIME_DIR / "private-media"
# Deliberately do not expose MEDIA_ROOT through any URL or static-file route.
MEDIA_URL = "/private-media-is-not-served/"
FILE_UPLOAD_PERMISSIONS = 0o600
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "workspace"
LOGOUT_REDIRECT_URL = "home"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Local development only. Provider calls also require the applicable journal or
# publisher policy and role, metadata consent where applicable, and confirmation.
IPS_OPENAI_ENABLED = os.environ.get("IPS_OPENAI_ENABLED") == "1"
IPS_OPENAI_MODEL = os.environ.get("IPS_OPENAI_MODEL", "").strip()
IPS_OPENAI_IMAGE_ENABLED = os.environ.get("IPS_OPENAI_IMAGE_ENABLED") == "1"
IPS_OPENAI_IMAGE_MODEL = os.environ.get("IPS_OPENAI_IMAGE_MODEL", "").strip()

# Only for locally bound, fictional-data publishing demonstrations.
IPS_LOCAL_PUBLICATION_ENABLED = DEBUG and os.environ.get("IPS_LOCAL_PUBLICATION_ENABLED") == "1"
