from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include(("core.urls", "core"), namespace="core")),
    path("", include(("accounts.urls", "accounts"), namespace="accounts")),
    path("", include(("quotes.urls", "quotes"), namespace="quotes")),
]
