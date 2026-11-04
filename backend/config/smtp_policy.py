"""Offline validation of a possible Altavien SMTP configuration.

This module never connects to a server and never changes Django's active mail
backend. Configuration validity does not establish deliverability, domain
ownership or suitability for a real-data release.
"""
from dataclasses import dataclass, field
import ipaddress
import re
import unicodedata

from django.core.exceptions import ValidationError
from django.core.validators import validate_email


class SMTPConfigurationError(ValueError):
    """A configuration error that never includes the supplied value."""


def _required(values, name, *, maximum=4096, allow_outer_space=False):
    value = values.get(name, "")
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(unicodedata.category(char) in ("Cc", "Cs", "Zl", "Zp") for char in value)
            or (not allow_outer_space and value != value.strip())):
        raise SMTPConfigurationError(f"Invalid or missing {name}.")
    return value


def _number(values, name, minimum, maximum, *, default=None):
    raw = values.get(name, default)
    if (not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal()
            or len(raw) > 5 or not minimum <= int(raw) <= maximum):
        raise SMTPConfigurationError(f"Invalid or missing {name}.")
    return int(raw)


def _hostname(value):
    if len(value) > 253 or not value.isascii():
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        return False
    labels = value.split(".")
    label = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
    return (len(labels) >= 2 and all(label.fullmatch(item) for item in labels)
            and bool(re.fullmatch(r"(?:[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{2,59})", labels[-1], re.IGNORECASE)))


@dataclass(frozen=True)
class SMTPPolicy:
    host: str
    port: int
    security: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    from_address: str = field(repr=False)
    timeout: int = 15

    @classmethod
    def from_mapping(cls, values):
        host = _required(values, "ALTAVIEN_SMTP_HOST", maximum=253)
        if not _hostname(host):
            raise SMTPConfigurationError("Invalid ALTAVIEN_SMTP_HOST.")
        port = _number(values, "ALTAVIEN_SMTP_PORT", 1, 65535)
        security = _required(values, "ALTAVIEN_SMTP_SECURITY", maximum=8).lower()
        if security not in ("starttls", "tls"):
            raise SMTPConfigurationError("ALTAVIEN_SMTP_SECURITY must be starttls or tls.")
        username = _required(values, "ALTAVIEN_SMTP_USERNAME", maximum=254)
        password = _required(values, "ALTAVIEN_SMTP_PASSWORD", allow_outer_space=True)
        from_address = _required(values, "ALTAVIEN_SMTP_FROM", maximum=254)
        try:
            validate_email(from_address)
        except ValidationError:
            raise SMTPConfigurationError("Invalid ALTAVIEN_SMTP_FROM.") from None
        domain = from_address.rsplit("@", 1)[-1].lower()
        if domain != "altavien.com" and not domain.endswith(".altavien.com"):
            raise SMTPConfigurationError("ALTAVIEN_SMTP_FROM must use the Altavien domain.")
        timeout = _number(values, "ALTAVIEN_SMTP_TIMEOUT", 1, 120, default="15")
        return cls(host=host.lower(), port=port, security=security, username=username,
                   password=password, from_address=from_address, timeout=timeout)

    def backend_options(self):
        """Return Django 5.2 SMTP options for future reviewed integration.

        The returned mapping contains credentials. Never log or display it.
        Constructing or using a backend is outside this validation module.
        """
        return {"host": self.host, "port": self.port, "username": self.username,
                "password": self.password, "timeout": self.timeout,
                "use_tls": self.security == "starttls", "use_ssl": self.security == "tls",
                "fail_silently": False}
