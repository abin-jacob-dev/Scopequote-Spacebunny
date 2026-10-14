from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import django

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "scopequote.settings")
django.setup()

E2E_USER_EMAIL = (
    os.getenv("E2E_USER_EMAIL", "e2e-owner@example.test").strip().casefold()
)
E2E_ALLOWED_USER_EMAILS = frozenset({E2E_USER_EMAIL})
E2E_QUOTE_TITLE = os.getenv("E2E_QUOTE_TITLE", "E2E publication fixture")
E2E_CLIENT_IP = os.getenv("E2E_CLIENT_IP", "127.0.0.1")
E2E_CLIENT_IPS = frozenset(
    item.strip()
    for item in os.getenv("E2E_CLIENT_IPS", "127.0.0.1,::1").split(",")
    if item.strip()
)
_E2E_RUN_ID_ERROR = (
    "E2E_RUN_ID must be 1-64 lowercase ASCII letters or digits "
    "separated by single hyphens."
)
_E2E_RUN_ID_MIN_LENGTH = 1
_E2E_RUN_ID_MAX_LENGTH = 64
_E2E_RUN_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", re.ASCII)
_E2E_STORAGE_LEAVES = {
    "E2E_MEDIA_ROOT": "media",
    "E2E_STATIC_ROOT": "static",
    "E2E_OUTPUT_DIR": "playwright",
    "E2E_IDENTITY_ROOT": "identities",
}
_E2E_IDENTITY_FILE = "emails.log"
_E2E_GUEST_SESSION_KEYS = frozenset(
    {
        "scopequote.guest_estimate",
        "scopequote.guest_quote",
        "scopequote.conversion_nonce",
    }
)


def _assert_run_id(run_id: str) -> None:
    if (
        not isinstance(run_id, str)
        or not run_id.isascii()
        or not _E2E_RUN_ID_MIN_LENGTH <= len(run_id) <= _E2E_RUN_ID_MAX_LENGTH
        or _E2E_RUN_ID_PATTERN.fullmatch(run_id) is None
    ):
        raise RuntimeError(_E2E_RUN_ID_ERROR)


def _password() -> str:
    value = os.getenv("E2E_USER_PASSWORD")
    if value:
        return value
    value = f"E2E-{secrets.token_urlsafe(24)}-Aa!"
    os.environ["E2E_USER_PASSWORD"] = value
    return value


def _require_configuration() -> None:
    _assert_run_id(os.getenv("E2E_RUN_ID", ""))
    database_name = os.getenv("E2E_DATABASE_NAME", "").strip()
    if not database_name:
        raise RuntimeError("E2E_DATABASE_NAME must name a dedicated database.")
    if database_name.casefold() in {"postgres", "template0", "template1"}:
        raise RuntimeError("E2E_DATABASE_NAME must identify a dedicated database.")
    if os.getenv("E2E_ALLOW_DESTRUCTIVE_RESET") != "1":
        raise RuntimeError(
            "E2E_ALLOW_DESTRUCTIVE_RESET=1 is required before E2E cleanup."
        )
    if os.getenv("SCOPEQUOTE_ENV") != "test":
        raise RuntimeError("E2E cleanup requires SCOPEQUOTE_ENV=test.")

    from django.db import connection

    if str(connection.settings_dict.get("NAME")) != database_name:
        raise RuntimeError("The active database does not match E2E_DATABASE_NAME.")


def _temporary_base() -> Path:
    temporary_root = Path(tempfile.gettempdir()).resolve()
    filesystem_root = Path(temporary_root.anchor)
    project_root = PROJECT_ROOT.resolve()
    if (
        temporary_root == filesystem_root
        or temporary_root == project_root
        or temporary_root.is_relative_to(project_root)
        or project_root.is_relative_to(temporary_root)
    ):
        raise RuntimeError(
            "E2E temporary roots must be outside the filesystem and project roots."
        )
    return temporary_root


def _isolated_storage_roots() -> dict[str, Path]:
    run_id = os.getenv("E2E_RUN_ID", "")
    _assert_run_id(run_id)
    temporary_root = _temporary_base()
    e2e_base = temporary_root / "scopequote-e2e"
    expected_root = e2e_base / run_id
    for candidate, label in (
        (e2e_base, "E2E temporary base"),
        (expected_root, "E2E run root"),
    ):
        if candidate.resolve() != candidate:
            raise RuntimeError(f"{label} must not traverse a symlink.")
    configured_root = os.getenv("E2E_RUN_ROOT", "").strip()
    if configured_root and Path(configured_root).resolve() != expected_root:
        raise RuntimeError("E2E_RUN_ROOT does not match the isolated run directory.")

    roots = {}
    for name, leaf in _E2E_STORAGE_LEAVES.items():
        value = os.getenv(name, "").strip()
        if not value:
            raise RuntimeError(f"{name} must be set for E2E cleanup.")
        root = Path(value).resolve()
        expected_path = expected_root / leaf
        if root != expected_path:
            raise RuntimeError(f"{name} must be inside the isolated run directory.")
        roots[name] = root

    registry_value = os.getenv("E2E_IDENTITY_REGISTRY", "").strip()
    expected_registry = expected_root / "identities" / _E2E_IDENTITY_FILE
    if not registry_value or Path(registry_value).resolve() != expected_registry:
        raise RuntimeError(
            "E2E_IDENTITY_REGISTRY must be inside the isolated run directory."
        )
    return roots


def _registered_user_emails(
    storage_roots: dict[str, Path], run_id: str
) -> frozenset[str]:
    registry = storage_roots["E2E_IDENTITY_ROOT"] / _E2E_IDENTITY_FILE
    emails = set(E2E_ALLOWED_USER_EMAILS)
    if not registry.exists():
        return frozenset(emails)

    pattern = re.compile(rf"e2e-account-{re.escape(run_id)}-[a-z0-9-]+@example\.test")
    for line in registry.read_text(encoding="utf-8").splitlines():
        email = line.strip().casefold()
        if not email:
            continue
        if not pattern.fullmatch(email):
            raise RuntimeError("E2E identity registry contains an invalid identity.")
        emails.add(email)
    return frozenset(emails)


def _e2e_user_ids(user_emails: frozenset[str]) -> list[object]:
    from accounts.models import User

    return list(
        User._base_manager.filter(email__in=user_emails).values_list("pk", flat=True)
    )


def _e2e_storage_keys(user_ids: list[object]) -> set[str]:
    from accounts.models import Profile
    from quotes.models import Quote, QuoteVersion

    keys = set(
        Profile._base_manager.filter(user_id__in=user_ids)
        .exclude(logo="")
        .values_list("logo", flat=True)
    )
    keys.update(
        Quote._base_manager.filter(owner_id__in=user_ids)
        .exclude(logo_object_key="")
        .values_list("logo_object_key", flat=True)
    )
    keys.update(
        QuoteVersion._base_manager.filter(quote__owner_id__in=user_ids)
        .exclude(logo_object_key="")
        .values_list("logo_object_key", flat=True)
    )
    return {str(key) for key in keys if key}


def _e2e_rate_limit_keys(user_emails: frozenset[str]) -> set[str]:
    from accounts.services import hash_rate_limit_key

    client_ips = set(E2E_CLIENT_IPS)
    client_ips.add(E2E_CLIENT_IP)
    routes = (
        "accounts.login",
        "accounts.signup",
        "accounts.password-reset",
        "accounts.verification-resend",
    )
    keys = {
        f"{route}:{client_ip}:{email}"
        for route in routes
        for client_ip in client_ips
        for email in user_emails
    }
    keys.update(
        f"{route}:ip:{client_ip}" for route in routes for client_ip in client_ips
    )
    keys.update(f"{route}:account:{email}" for route in routes for email in user_emails)
    keys.update(
        f"accounts.password-reset-confirm:{client_ip}" for client_ip in client_ips
    )
    keys.update(
        f"accounts.password-reset-confirm:ip:{client_ip}" for client_ip in client_ips
    )
    keys.update(
        f"accounts.password-reset-confirm:account:{email}" for email in user_emails
    )
    from quotes.models import ShareLink
    from quotes.services.links import decrypt_link_token
    from quotes.services.tokens import hash_token

    user_ids = _e2e_user_ids(user_emails)
    keys.update(f"quotes.guest-pdf:{client_ip}" for client_ip in client_ips)
    keys.update(f"quotes.owner-pdf:{user_id}" for user_id in user_ids)
    for link in ShareLink.objects.filter(quote_version__quote__owner_id__in=user_ids):
        try:
            token_hash = hash_token(decrypt_link_token(link))
        except Exception:
            continue
        keys.update(
            f"quotes.public-response:{client_ip}:{token_hash}"
            for client_ip in client_ips
        )
    return {hash_rate_limit_key(key) for key in keys}


def cleanup(*, remove_storage: bool = True) -> None:
    from django.contrib.sessions.backends.db import SessionStore
    from django.contrib.sessions.models import Session
    from django.db import transaction

    from accounts.models import DeletionTombstone, MediaCleanupTask, Profile, User
    from quotes.models import Quote, QuoteEvent, QuoteLineItem, QuoteVersion, ShareLink

    _require_configuration()
    storage_roots = _isolated_storage_roots()
    run_id = os.environ["E2E_RUN_ID"]
    user_emails = _registered_user_emails(storage_roots, run_id)
    user_ids = _e2e_user_ids(user_emails)
    storage_keys = _e2e_storage_keys(user_ids)
    rate_limit_keys = _e2e_rate_limit_keys(user_emails)
    with transaction.atomic():
        quote_ids = list(
            Quote._base_manager.filter(owner_id__in=user_ids).values_list(
                "pk", flat=True
            )
        )
        if quote_ids:
            QuoteEvent._base_manager.filter(quote_id__in=quote_ids).delete()
            ShareLink._base_manager.filter(
                quote_version__quote_id__in=quote_ids
            ).delete()
            QuoteVersion._base_manager.filter(quote_id__in=quote_ids).delete()
            QuoteLineItem._base_manager.filter(quote_id__in=quote_ids).delete()
            Quote._base_manager.filter(pk__in=quote_ids).delete()
        if storage_keys:
            MediaCleanupTask._base_manager.filter(storage_key__in=storage_keys).delete()
        if user_ids:
            DeletionTombstone._base_manager.filter(account_id__in=user_ids).delete()
            Profile._base_manager.filter(user_id__in=user_ids).delete()
            User._base_manager.filter(pk__in=user_ids).delete()

    expected_user_ids = {str(user_id) for user_id in user_ids}
    for session in Session.objects.exclude(session_key=None).iterator():
        try:
            data = SessionStore(session_key=session.session_key).load()
        except Exception:
            continue
        if str(data.get("_auth_user_id", "")) in expected_user_ids or any(
            key in data for key in _E2E_GUEST_SESSION_KEYS
        ):
            session.delete()

    if rate_limit_keys:
        from accounts.models import RateLimitBucket

        RateLimitBucket._base_manager.filter(key__in=rate_limit_keys).delete()

    if remove_storage:
        for root in storage_roots.values():
            if root.exists():
                shutil.rmtree(root)


def expire_fixture_links() -> None:
    from django.utils import timezone

    from quotes.models import Quote, ShareLink

    quote_ids = Quote.objects.filter(
        owner__email=E2E_USER_EMAIL,
        project_title=E2E_QUOTE_TITLE,
    ).values_list("pk", flat=True)
    ShareLink.objects.filter(quote_version__quote_id__in=quote_ids).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )


def seed() -> None:
    from decimal import Decimal

    from django.db import transaction
    from django.utils import timezone

    from accounts.models import Profile, User
    from quotes.models import Quote, QuoteLineItem

    _require_configuration()
    cleanup(remove_storage=False)
    with transaction.atomic():
        user, _created = User.objects.get_or_create(
            email=E2E_USER_EMAIL,
            defaults={"is_active": True},
        )
        user.is_active = True
        user.email_verified_at = timezone.now()
        user.last_activity_at = timezone.now()
        user.set_password(_password())
        user.save()
        Profile.create_for_user(user)
        quote = Quote.objects.create(
            owner=user,
            project_title=E2E_QUOTE_TITLE,
            project_description=(
                "A complete client project with a defined scope, delivery schedule, "
                "commercial terms, and a tested handoff."
            ),
            category="website",
            delivery_days=5,
            complexity="standard",
            revision_rounds=0,
            urgency="standard",
            experience="intermediate",
            currency="USD",
            effective_hours=Decimal("20.00"),
            minimum=Decimal("1000.00"),
            recommended=Decimal("1000.00"),
            maximum=Decimal("1000.00"),
            chosen_total=Decimal("1000.00"),
            hourly_equivalent=Decimal("50.00"),
            client_name="E2E Client",
            client_email="e2e-client@example.test",
            sender_name="E2E Sender",
            sender_business_name="E2E Studio",
            sender_email=E2E_USER_EMAIL,
            issue_date=timezone.localdate(),
            valid_until=timezone.localdate() + timedelta(days=30),
            deposit_percentage=50,
            timeline="Five focused delivery days.",
            revision_terms="One revision round is included.",
            terms="Payment follows the agreed schedule.",
        )
        QuoteLineItem.objects.create(
            quote=quote,
            label="Project fee",
            description="A complete project delivery.",
            amount=Decimal("1000.00"),
            sort_order=0,
        )
        quote.recalculate_line_item_total()
        quote.save()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("seed", "cleanup", "expire-fixture-links"))
    arguments = parser.parse_args()
    if arguments.action == "seed":
        seed()
    elif arguments.action == "cleanup":
        cleanup()
    else:
        expire_fixture_links()


if __name__ == "__main__":
    main()
