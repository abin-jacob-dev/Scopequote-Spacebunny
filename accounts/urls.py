from django.urls import path

from accounts import views

app_name = "accounts"

urlpatterns = [
    path("accounts/login/", views.LoginView.as_view(), name="login"),
    path("accounts/logout/", views.LogoutView.as_view(), name="logout"),
    path("accounts/signup/", views.SignupView.as_view(), name="signup"),
    path(
        "accounts/verify/",
        views.VerificationNoticeView.as_view(),
        name="verify-notice",
    ),
    path(
        "accounts/verify/",
        views.VerificationNoticeView.as_view(),
        name="verify_notice",
    ),
    path(
        "accounts/verify/<str:uidb64>/<str:token>/",
        views.VerifyEmailView.as_view(),
        name="verify",
    ),
    path(
        "accounts/password-reset/",
        views.PasswordResetRequestView.as_view(),
        name="password-reset",
    ),
    path(
        "accounts/password-reset/",
        views.PasswordResetRequestView.as_view(),
        name="password_reset",
    ),
    path(
        "accounts/password-reset/done/",
        views.PasswordResetDoneView.as_view(),
        name="password-reset-done",
    ),
    path(
        "accounts/password-reset/done/",
        views.PasswordResetDoneView.as_view(),
        name="password_reset_done",
    ),
    path(
        "accounts/password-reset/complete/",
        views.PasswordResetCompleteView.as_view(),
        name="password-reset-complete",
    ),
    path(
        "accounts/password-reset/complete/",
        views.PasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
    path(
        "accounts/password-reset/<str:uidb64>/<str:token>/",
        views.PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path(
        "accounts/password-reset/<str:uidb64>/<str:token>/",
        views.PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path("settings/profile/", views.ProfileView.as_view(), name="profile"),
    path("settings/security/", views.SecurityView.as_view(), name="security"),
    path("settings/delete/", views.DeleteAccountView.as_view(), name="delete"),
    path("dashboard/", views.DashboardView.as_view(), name="dashboard"),
    path("quotes/", views.QuoteListView.as_view(), name="quote-list"),
    path("quotes/", views.QuoteListView.as_view(), name="quote_list"),
]
