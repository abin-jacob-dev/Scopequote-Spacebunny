import re
import secrets

from django.conf import settings
from django.middleware.csrf import CsrfViewMiddleware

CSP = (
    "default-src 'self'; base-uri 'self'; object-src 'none'; "
    "frame-ancestors 'none'; form-action 'self'; img-src 'self' data:; "
    "style-src 'self'; script-src 'self'; font-src 'self'; connect-src 'none'"
)
_CSP_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def new_csp_nonce() -> str:
    nonce = secrets.token_urlsafe(18)
    if not _CSP_NONCE.fullmatch(nonce):
        raise ValueError("The CSP nonce is invalid.")
    return nonce


def csp_policy(nonce: str) -> str:
    if not isinstance(nonce, str) or not _CSP_NONCE.fullmatch(nonce):
        raise ValueError("The CSP nonce is invalid.")
    return CSP.replace("style-src 'self';", f"style-src 'self' 'nonce-{nonce}';")


INTERACTIVE_PATH_PREFIXES = (
    "/admin",
    "/accounts",
    "/dashboard",
    "/estimator",
    "/quotes",
)


class ScopequoteCsrfViewMiddleware(CsrfViewMiddleware):
    def _origin_verified(self, request):
        if (
            request.path.startswith("/q/")
            and request.META.get("HTTP_ORIGIN") == "null"
            and request.META.get("HTTP_SEC_FETCH_SITE") == "same-origin"
            and request.META.get("HTTP_SEC_FETCH_MODE") == "navigate"
            and request.META.get("HTTP_SEC_FETCH_DEST") == "document"
        ):
            return True
        return super()._origin_verified(request)


class PrivateContentHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        content_type = response.get("Content-Type", "")
        is_html = content_type.startswith("text/html")
        user = getattr(request, "user", None)
        authenticated = bool(user and user.is_authenticated)
        guest_state = any(
            key in getattr(request, "session", {})
            for key in (
                "scopequote.guest_estimate",
                "scopequote.guest_quote",
                "scopequote.conversion_nonce",
            )
        )
        private_path = request.path.startswith("/q/") or request.path.endswith(
            "/links/"
        )
        if is_html and (authenticated or guest_state or private_path):
            response["Cache-Control"] = "no-store"
            if request.path.startswith("/q/"):
                response["Referrer-Policy"] = "no-referrer"
            else:
                response.setdefault("Referrer-Policy", "same-origin")
        return response


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.csp_nonce = new_csp_nonce()
        response = self.get_response(request)
        response.setdefault("X-Content-Type-Options", "nosniff")
        response.setdefault("X-Frame-Options", "DENY")
        if request.path == "/q" or request.path.startswith("/q/"):
            response["Referrer-Policy"] = "no-referrer"
        elif any(
            request.path == prefix or request.path.startswith(f"{prefix}/")
            for prefix in INTERACTIVE_PATH_PREFIXES
        ):
            response.setdefault("Referrer-Policy", "same-origin")
        else:
            response.setdefault(
                "Referrer-Policy",
                getattr(settings, "SECURE_REFERRER_POLICY", "no-referrer"),
            )
        response["Content-Security-Policy"] = csp_policy(request.csp_nonce)
        hsts_seconds = int(getattr(settings, "SECURE_HSTS_SECONDS", 0) or 0)
        if hsts_seconds > 0:
            value = str(hsts_seconds)
            if getattr(settings, "SECURE_HSTS_INCLUDE_SUBDOMAINS", False):
                value += "; includeSubDomains"
            if getattr(settings, "SECURE_HSTS_PRELOAD", False):
                value += "; preload"
            response.setdefault("Strict-Transport-Security", value)
        if request.path.startswith("/q/") and "Cache-Control" not in response:
            response["Cache-Control"] = "no-store"
        return response
