from django.urls import path

from core import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("legal/privacy/", views.privacy, name="privacy"),
    path("legal/terms/", views.terms, name="terms"),
    path("health/", views.health, name="health"),
    path("ready/", views.readiness, name="readiness"),
]
