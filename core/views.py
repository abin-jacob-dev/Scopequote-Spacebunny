from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render


def home(request) -> HttpResponse:
    return render(request, "core/home.html")


def privacy(request) -> HttpResponse:
    return render(
        request,
        "core/legal/privacy.html",
        {"support_email": settings.PUBLIC_SUPPORT_EMAIL},
    )


def terms(request) -> HttpResponse:
    return render(
        request,
        "core/legal/terms.html",
        {"support_email": settings.PUBLIC_SUPPORT_EMAIL},
    )


def health(request) -> HttpResponse:
    return HttpResponse("ok", content_type="text/plain; charset=utf-8")


def readiness(request) -> HttpResponse:
    from accounts.services import restore_reconciliation_pending

    try:
        pending = restore_reconciliation_pending()
    except Exception:
        return HttpResponse(
            "restore reconciliation is required",
            content_type="text/plain; charset=utf-8",
            status=503,
        )
    if pending:
        return HttpResponse(
            "restore reconciliation is required",
            content_type="text/plain; charset=utf-8",
            status=503,
        )
    return HttpResponse("ready", content_type="text/plain; charset=utf-8")
