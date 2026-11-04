"""Disposable, fictional-data PostgreSQL compatibility tests only.

This profile is limited to Django's test runner and a freshly created database
name. It is not a production configuration and must never serve HTTP traffic.
"""
import os
import pwd
import re
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

if Path(sys.argv[0]).name != "manage.py" or sys.argv[1:2] != ["test"]:
    raise ImproperlyConfigured("The PostgreSQL probe profile is limited to manage.py test.")
if os.environ.get("ALTAVIEN_PROBE_CONFIRM_SYNTHETIC") != "1":
    raise ImproperlyConfigured("The PostgreSQL probe requires fictional test data.")
try:
    postgres_uid = pwd.getpwnam("postgres").pw_uid
except KeyError as exc:
    raise ImproperlyConfigured("The postgres operating-system account is unavailable.") from exc
if os.geteuid() != postgres_uid:
    raise ImproperlyConfigured("The PostgreSQL probe must use the local postgres account.")

probe_name = os.environ.get("ALTAVIEN_PROBE_DB", "")
if not re.fullmatch(r"altavien_probe_[0-9a-f]{24}", probe_name):
    raise ImproperlyConfigured("Invalid isolated PostgreSQL probe database name.")

from .settings import *  # noqa: F403,E402

DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": probe_name,
    "USER": "postgres",
    "HOST": "/var/run/postgresql",
    "PORT": "5432",
    "TEST": {"NAME": probe_name + "_test"},
}}
ALLOWED_HOSTS = ["testserver"]
EMAIL_BACKEND = "config.unavailable_email.EmailBackend"
IPS_OPENAI_ENABLED = False
IPS_OPENAI_MODEL = ""
IPS_OPENAI_IMAGE_ENABLED = False
IPS_OPENAI_IMAGE_MODEL = ""
IPS_LOCAL_PUBLICATION_ENABLED = True
