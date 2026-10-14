from django.urls import path

from quotes import views

app_name = "quotes"

urlpatterns = [
    path("estimator/", views.estimator, name="estimator"),
    path("quotes/guest/", views.guest_quote, name="guest_quote"),
    path("quotes/guest.pdf/", views.guest_quote_pdf, name="guest_pdf"),
    path("quotes/guest.pdf/", views.guest_quote_pdf, name="guest-pdf"),
    path("q/<str:token>/", views.public_interstitial, name="public_interstitial"),
    path("q/<str:token>/", views.public_interstitial, name="public-interstitial"),
    path("q/<str:token>/open/", views.public_open, name="public_open"),
    path("q/<str:token>/open/", views.public_open, name="public-open"),
    path("q/<str:token>/logo/", views.public_version_logo, name="public-logo"),
    path("q/<str:token>/logo/", views.public_version_logo, name="public-logo-route"),
    path("q/<str:token>/view/", views.public_quote, name="public_quote"),
    path("q/<str:token>/view/", views.public_quote, name="public-quote"),
    path(
        "q/<str:token>/response/",
        views.public_response,
        name="public_response",
    ),
    path(
        "q/<str:token>/response/",
        views.public_response,
        name="public-response",
    ),
    path("quotes/new/", views.new_quote, name="new"),
    path("quotes/new/", views.new_quote, name="quote-new"),
    path("quotes/<uuid:pk>/", views.quote_detail, name="detail"),
    path("quotes/<uuid:pk>/", views.quote_detail, name="quote-detail"),
    path("quotes/<uuid:pk>/pdf/", views.quote_pdf, name="pdf"),
    path("quotes/<uuid:pk>/pdf/", views.quote_pdf, name="quote-pdf"),
    path(
        "quotes/versions/<uuid:version_id>/logo/",
        views.version_logo,
        name="version-logo",
    ),
    path(
        "quotes/versions/<uuid:version_id>/logo/",
        views.version_logo,
        name="version_logo",
    ),
    path("quotes/<uuid:pk>/edit/", views.edit_quote, name="edit"),
    path("quotes/<uuid:pk>/edit/", views.edit_quote, name="quote-edit"),
    path("quotes/<uuid:pk>/publish/", views.publish_quote, name="publish"),
    path("quotes/<uuid:pk>/publish/", views.publish_quote, name="quote-publish"),
    path("quotes/<uuid:pk>/duplicate/", views.duplicate_quote_view, name="duplicate"),
    path(
        "quotes/<uuid:pk>/duplicate/",
        views.duplicate_quote_view,
        name="quote-duplicate",
    ),
    path("quotes/<uuid:pk>/links/", views.link_management, name="links"),
    path("quotes/<uuid:pk>/links/", views.link_management, name="quote-links"),
]
