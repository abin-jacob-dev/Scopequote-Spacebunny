from __future__ import annotations

import base64
import binascii
import os
import secrets
from pathlib import Path
from urllib.parse import unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import validate_email

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ImproperlyConfigured(f"{name} must be a boolean value.")


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default

    try:
        return int(value)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be an integer.") from exc


def env_list(name: str, default: str = "") -> list[str]:
    value = os.getenv(name, default)
    return [item.strip() for item in value.split(",") if item.strip()]


def env_path(name: str, default: str) -> Path:
    value = Path(os.getenv(name, default))
    return value if value.is_absolute() else BASE_DIR / value


def database_from_url(database_url: str) -> dict[str, object]:
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ImproperlyConfigured("DATABASE_URL must use postgres or postgresql.")

    database_name = unquote(parsed.path.lstrip("/")) or os.getenv("POSTGRES_DB", "")
    database_user = unquote(parsed.username or "") or os.getenv("POSTGRES_USER", "")
    database_password = unquote(parsed.password or "") or os.getenv(
        "POSTGRES_PASSWORD", ""
    )
    database_host = parsed.hostname or os.getenv("POSTGRES_HOST", "")
    database_port = str(parsed.port or os.getenv("POSTGRES_PORT", "5432"))

    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": database_name,
        "USER": database_user,
        "PASSWORD": database_password,
        "HOST": database_host,
        "PORT": database_port,
        "CONN_MAX_AGE": env_int("CONN_MAX_AGE", 60),
        "CONN_HEALTH_CHECKS": True,
    }


def share_token_encryption_key() -> str:
    value = os.getenv("SHARE_TOKEN_ENCRYPTION_KEY", "")
    if not value:
        return ""

    try:
        decoded_key = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImproperlyConfigured(
            "SHARE_TOKEN_ENCRYPTION_KEY must be base64-encoded."
        ) from exc

    if len(decoded_key) != 32:
        raise ImproperlyConfigured(
            "SHARE_TOKEN_ENCRYPTION_KEY must decode to exactly 32 bytes."
        )
    return value


SCOPEQUOTE_ENV = os.getenv("SCOPEQUOTE_ENV", "local").strip().lower()
if SCOPEQUOTE_ENV not in {"local", "development", "test", "production"}:
    raise ImproperlyConfigured(
        "SCOPEQUOTE_ENV must be local, development, test, or production."
    )

IS_PRODUCTION = SCOPEQUOTE_ENV == "production"
DEBUG = env_bool("DEBUG", False)
if IS_PRODUCTION and DEBUG:
    raise ImproperlyConfigured("DEBUG must be false in production.")

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    if SCOPEQUOTE_ENV == "production":
        raise ImproperlyConfigured("SECRET_KEY must be set in production.")
    SECRET_KEY = secrets.token_urlsafe(50)

ALLOWED_HOSTS = env_list(
    "ALLOWED_HOSTS", "localhost,127.0.0.1" if SCOPEQUOTE_ENV != "production" else ""
)
if not ALLOWED_HOSTS and (not DEBUG or SCOPEQUOTE_ENV == "production"):
    raise ImproperlyConfigured("ALLOWED_HOSTS must be set outside local development.")

CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL:
    DATABASES = {"default": database_from_url(DATABASE_URL)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv(
                "POSTGRES_DB", "postgres" if SCOPEQUOTE_ENV != "production" else ""
            ),
            "USER": os.getenv(
                "POSTGRES_USER", "postgres" if SCOPEQUOTE_ENV != "production" else ""
            ),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
            "HOST": os.getenv("POSTGRES_HOST", "localhost"),
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": env_int("CONN_MAX_AGE", 60),
            "CONN_HEALTH_CHECKS": True,
        }
    }

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core.apps.CoreConfig",
    "accounts.apps.AccountsConfig",
    "pricing.apps.PricingConfig",
    "quotes.apps.QuotesConfig",
]

AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "core.middleware.SecurityHeadersMiddleware",
]
USE_WHITENOISE = env_bool("USE_WHITENOISE", IS_PRODUCTION)
if USE_WHITENOISE:
    MIDDLEWARE.append("whitenoise.middleware.WhiteNoiseMiddleware")
MIDDLEWARE.extend(
    [
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.common.CommonMiddleware",
        "core.middleware.ScopequoteCsrfViewMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "core.middleware.PrivateContentHeadersMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
        "django.middleware.clickjacking.XFrameOptionsMiddleware",
    ]
)

ROOT_URLCONF = "scopequote.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "scopequote.wsgi.application"
ASGI_APPLICATION = "scopequote.asgi.application"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            + "UserAttributeSimilarityValidator"
        ),
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = env_path("STATIC_ROOT", "staticfiles")
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = env_path("MEDIA_ROOT", "media")
WHITENOISE_USE_FINDERS = DEBUG

SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", IS_PRODUCTION)
SESSION_COOKIE_HTTPONLY = env_bool("SESSION_COOKIE_HTTPONLY", True)
SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
SESSION_COOKIE_AGE = env_int("SESSION_COOKIE_AGE", 60 * 60 * 24 * 14)
SESSION_EXPIRE_AT_BROWSER_CLOSE = env_bool("SESSION_EXPIRE_AT_BROWSER_CLOSE", False)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", IS_PRODUCTION)
CSRF_COOKIE_HTTPONLY = env_bool("CSRF_COOKIE_HTTPONLY", IS_PRODUCTION)
if IS_PRODUCTION:
    if not SESSION_COOKIE_SECURE:
        raise ImproperlyConfigured("SESSION_COOKIE_SECURE must be true in production.")
    if not CSRF_COOKIE_SECURE:
        raise ImproperlyConfigured("CSRF_COOKIE_SECURE must be true in production.")
    if not SESSION_COOKIE_HTTPONLY:
        raise ImproperlyConfigured(
            "SESSION_COOKIE_HTTPONLY must be true in production."
        )
    if not CSRF_COOKIE_HTTPONLY:
        raise ImproperlyConfigured("CSRF_COOKIE_HTTPONLY must be true in production.")
    if SESSION_COOKIE_SAMESITE not in {"Lax", "Strict"}:
        raise ImproperlyConfigured("SESSION_COOKIE_SAMESITE must be Lax or Strict.")

TRUST_PROXY_HEADERS = env_bool("TRUST_PROXY_HEADERS", False)
proxy_header = os.getenv("SECURE_PROXY_SSL_HEADER") if TRUST_PROXY_HEADERS else None
if TRUST_PROXY_HEADERS and not proxy_header:
    raise ImproperlyConfigured(
        "SECURE_PROXY_SSL_HEADER must be set when TRUST_PROXY_HEADERS is true."
    )
if proxy_header:
    proxy_parts = [part.strip() for part in proxy_header.split(",", maxsplit=1)]
    if len(proxy_parts) != 2 or not all(proxy_parts):
        raise ImproperlyConfigured(
            "SECURE_PROXY_SSL_HEADER must be a comma-separated header and value."
        )
    SECURE_PROXY_SSL_HEADER = (proxy_parts[0], proxy_parts[1])
else:
    SECURE_PROXY_SSL_HEADER = None

SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", IS_PRODUCTION)
if IS_PRODUCTION and not SECURE_SSL_REDIRECT:
    raise ImproperlyConfigured("SECURE_SSL_REDIRECT must be true in production.")
SECURE_HSTS_SECONDS = env_int(
    "SECURE_HSTS_SECONDS",
    31536000 if IS_PRODUCTION else 0,
)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool(
    "SECURE_HSTS_INCLUDE_SUBDOMAINS",
    IS_PRODUCTION,
)
SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", IS_PRODUCTION)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = os.getenv("SECURE_REFERRER_POLICY", "no-referrer")
X_FRAME_OPTIONS = "DENY"
TRUSTED_PROXY_IPS = env_list("TRUSTED_PROXY_IPS")
PUBLIC_RESPONSE_RATE_LIMIT = env_int("PUBLIC_RESPONSE_RATE_LIMIT", 20)
PUBLIC_RESPONSE_RATE_WINDOW = env_int("PUBLIC_RESPONSE_RATE_WINDOW", 60)
PDF_RENDER_TIMEOUT_SECONDS = env_int("PDF_RENDER_TIMEOUT_SECONDS", 15)
PDF_RATE_LIMIT = env_int("PDF_RATE_LIMIT", 20)
PDF_RATE_WINDOW = env_int("PDF_RATE_WINDOW", 60)
CLEANUP_BATCH_SIZE = env_int("CLEANUP_BATCH_SIZE", 1000)

EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend"
    if DEBUG
    else "django.core.mail.backends.smtp.EmailBackend",
).strip()
EMAIL_HOST = os.getenv("EMAIL_HOST", "localhost").strip()
EMAIL_PORT = env_int("EMAIL_PORT", 25)
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", False)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
EMAIL_TIMEOUT = env_int("EMAIL_TIMEOUT", 10)
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "hello@example.com")
SERVER_EMAIL = os.getenv("SERVER_EMAIL", DEFAULT_FROM_EMAIL)
EMAIL_SENDER_VERIFIED = env_bool("EMAIL_SENDER_VERIFIED", False)
PUBLIC_SUPPORT_EMAIL = os.getenv("PUBLIC_SUPPORT_EMAIL", "support@scopequote.local")
if IS_PRODUCTION:
    if EMAIL_BACKEND != "django.core.mail.backends.smtp.EmailBackend":
        raise ImproperlyConfigured(
            "EMAIL_BACKEND must use the SMTP backend in production."
        )
    if not EMAIL_HOST:
        raise ImproperlyConfigured("EMAIL_HOST must be set in production.")
    if EMAIL_USE_TLS == EMAIL_USE_SSL:
        raise ImproperlyConfigured(
            "Production SMTP must enable exactly one of EMAIL_USE_TLS or EMAIL_USE_SSL."
        )
    for sender in (DEFAULT_FROM_EMAIL, SERVER_EMAIL):
        try:
            validate_email(sender)
        except ValidationError as exc:
            raise ImproperlyConfigured(
                "Production sender addresses must be valid."
            ) from exc
        domain = sender.rsplit("@", 1)[-1].casefold()
        if (
            domain
            in {
                "example.com",
                "example.net",
                "example.org",
                "example.test",
                "invalid",
                "localhost",
                "local",
            }
            or "replace" in sender.casefold()
        ):
            raise ImproperlyConfigured(
                "Production sender addresses must not use placeholders."
            )
    if not EMAIL_SENDER_VERIFIED:
        raise ImproperlyConfigured("EMAIL_SENDER_VERIFIED must be true in production.")

SHARE_TOKEN_ENCRYPTION_KEY = share_token_encryption_key()
if not SHARE_TOKEN_ENCRYPTION_KEY and SCOPEQUOTE_ENV == "production":
    raise ImproperlyConfigured("SHARE_TOKEN_ENCRYPTION_KEY must be set in production.")
SHARE_TOKEN_ENCRYPTION_KEY_VERSION = os.getenv(
    "SHARE_TOKEN_ENCRYPTION_KEY_VERSION", "v1"
)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
APPEND_SLASH = True
