from __future__ import annotations

from binascii import Error as BinasciiError

from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.views import LoginView as DjangoLoginView
from django.contrib.auth.views import LogoutView as DjangoLogoutView
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.db.models.deletion import ProtectedError
from django.http import HttpRequest
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from django.views.generic import FormView, TemplateView, UpdateView

from accounts.forms import (
    DeleteAccountForm,
    EmailAuthenticationForm,
    PasswordResetConfirmForm,
    PasswordResetRequestForm,
    ProfileForm,
    QuoteFilterForm,
    SecurityForm,
    SignupForm,
    VerificationNoticeForm,
    send_security_notice_email,
    send_verification_email,
)
from accounts.models import Profile, User
from accounts.services import (
    RateLimitExceeded,
    canonicalize_email,
    client_ip,
    enforce_rate_limit,
    record_authenticated_activity,
    request_account_deletion,
)
from quotes.models import Quote, QuoteStatus
from quotes.services.guest import (
    CONVERSION_NONCE_KEY,
    GUEST_ESTIMATE_KEY,
    GUEST_QUOTE_KEY,
    convert_guest_quote,
)

LOGIN_RATE_LIMIT = 10
AUTHENTICATION_RATE_WINDOW = 60
PASSWORD_RESET_RATE_LIMIT = 5
VERIFICATION_RESEND_RATE_LIMIT = PASSWORD_RESET_RATE_LIMIT
DASHBOARD_QUOTE_LIMIT = 8


def _rate_limit_keys(
    request: HttpRequest, *, route: str, email: str = ""
) -> tuple[str, str]:
    try:
        account_identifier = canonicalize_email(email)
    except ValidationError:
        account_identifier = "invalid"
    return (
        f"{route}:ip:{client_ip(request)}",
        f"{route}:account:{account_identifier}",
    )


def _enforce_auth_rate_limits(
    request: HttpRequest,
    *,
    route: str,
    limit: int,
    window_seconds: int,
    email: str = "",
) -> None:
    ip_key, account_key = _rate_limit_keys(request, route=route, email=email)
    enforce_rate_limit(key=ip_key, limit=limit, window_seconds=window_seconds)
    enforce_rate_limit(key=account_key, limit=limit, window_seconds=window_seconds)


def _rate_limited_response(request: HttpRequest, form, template_name: str, error):
    if not hasattr(form, "cleaned_data"):
        form.cleaned_data = {}
    form.add_error(None, str(error))
    response = render(
        request,
        template_name,
        {"form": form, "rate_limited": True},
        status=429,
    )
    response["Retry-After"] = str(error.retry_after)
    return response


def _convert_guest_after_authentication(request: HttpRequest, user: User) -> None:
    if not any(
        key in request.session
        for key in (GUEST_ESTIMATE_KEY, GUEST_QUOTE_KEY, CONVERSION_NONCE_KEY)
    ):
        return
    converted = convert_guest_quote(request, user)
    if converted is None:
        messages.warning(
            request,
            "Your account is ready, but the guest quotation could not be attached. "
            "Please try again from the estimator.",
        )
    else:
        record_authenticated_activity(user)
        messages.success(
            request,
            "Your guest quotation is now saved to your account.",
        )


def _user_from_uid(uidb64: str):
    if not isinstance(uidb64, str):
        return None
    try:
        pk = force_str(urlsafe_base64_decode(uidb64))
        return User._default_manager.get(pk=pk)
    except (
        BinasciiError,
        ValidationError,
        ValueError,
        TypeError,
        UnicodeError,
        User.DoesNotExist,
    ):
        return None


def _token_is_valid(user, token) -> bool:
    if user is None or not isinstance(token, str):
        return False
    try:
        return bool(default_token_generator.check_token(user, token))
    except (BinasciiError, ValidationError, ValueError, TypeError, UnicodeError):
        return False


class LoginView(DjangoLoginView):
    template_name = "accounts/login.html"
    form_class = EmailAuthenticationForm
    redirect_authenticated_user = False
    success_url = reverse_lazy("accounts:dashboard")

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST":
            try:
                _enforce_auth_rate_limits(
                    request,
                    route="accounts.login",
                    limit=LOGIN_RATE_LIMIT,
                    window_seconds=AUTHENTICATION_RATE_WINDOW,
                    email=request.POST.get("email", ""),
                )
            except RateLimitExceeded as error:
                return _rate_limited_response(
                    request,
                    self.get_form(),
                    self.template_name,
                    error,
                )
        if request.user.is_authenticated and request.method != "POST":
            return redirect(self.get_success_url())
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        _convert_guest_after_authentication(self.request, form.get_user())
        return response

    def get_success_url(self):
        redirect_to = self.request.POST.get(
            self.redirect_field_name
        ) or self.request.GET.get(self.redirect_field_name)
        if redirect_to and self.get_success_url_allowed(redirect_to):
            return redirect_to
        return str(self.success_url)

    def get_success_url_allowed(self, url):
        from django.utils.http import url_has_allowed_host_and_scheme

        return url_has_allowed_host_and_scheme(
            url,
            allowed_hosts={self.request.get_host()},
            require_https=self.request.is_secure(),
        )


class LogoutView(DjangoLogoutView):
    next_page = reverse_lazy("core:home")
    http_method_names = ("post", "options")


class SignupView(FormView):
    template_name = "accounts/signup.html"
    form_class = SignupForm
    success_url = reverse_lazy("accounts:verify-notice")

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST":
            try:
                _enforce_auth_rate_limits(
                    request,
                    route="accounts.signup",
                    limit=PASSWORD_RESET_RATE_LIMIT,
                    window_seconds=AUTHENTICATION_RATE_WINDOW,
                    email=request.POST.get("email", ""),
                )
            except RateLimitExceeded as error:
                return _rate_limited_response(
                    request,
                    self.get_form(),
                    self.template_name,
                    error,
                )
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        if getattr(form, "duplicate_email", False):
            messages.info(
                self.request,
                "If an account was created, check your email to verify it.",
            )
            return redirect(self.success_url)
        try:
            with transaction.atomic():
                user = form.save()
                Profile.create_for_user(user)
                send_verification_email(user=user, request=self.request)
        except IntegrityError:
            messages.info(
                self.request,
                "If an account was created, check your email to verify it.",
            )
            return redirect(self.success_url)
        except Exception:
            form.add_error(
                None,
                "We could not create your account right now. Please try again.",
            )
            return self.form_invalid(form)
        auth_login(
            self.request,
            user,
            backend="django.contrib.auth.backends.ModelBackend",
        )
        _convert_guest_after_authentication(self.request, user)
        messages.success(
            self.request,
            (
                "Your account was created. Check your email to verify it "
                "before publishing."
            ),
        )
        return redirect(self.success_url)

    def form_invalid(self, form):
        try:
            submitted_email = canonicalize_email(self.request.POST.get("email", ""))
        except ValidationError:
            submitted_email = ""
        if (
            submitted_email
            and User.objects.filter(email__iexact=submitted_email).exists()
        ):
            messages.info(
                self.request,
                "If an account was created, check your email to verify it.",
            )
            return redirect(self.success_url)
        return super().form_invalid(form)


class VerificationNoticeView(TemplateView):
    template_name = "accounts/verify.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["email"] = (
            self.request.user.email if self.request.user.is_authenticated else ""
        )
        context["verified"] = (
            self.request.user.is_email_verified
            if self.request.user.is_authenticated
            else False
        )
        context["form"] = VerificationNoticeForm(initial={"email": context["email"]})
        return context

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST":
            try:
                _enforce_auth_rate_limits(
                    request,
                    route="accounts.verification-resend",
                    limit=VERIFICATION_RESEND_RATE_LIMIT,
                    window_seconds=AUTHENTICATION_RATE_WINDOW,
                    email=request.POST.get("email", ""),
                )
            except RateLimitExceeded as error:
                return _rate_limited_response(
                    request,
                    VerificationNoticeForm(),
                    self.template_name,
                    error,
                )
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        form = VerificationNoticeForm(request.POST)
        if not form.is_valid():
            context = self.get_context_data()
            context["form"] = form
            return self.render_to_response(context, status=200)

        submitted_email = form.cleaned_data["email"]
        user = User.objects.filter(
            email=submitted_email,
            is_active=True,
        ).first()
        if user is not None and not user.is_email_verified:
            try:
                send_verification_email(user=user, request=request)
            except Exception:
                pass
        messages.info(
            request,
            "If an active account needs verification, a new link is on its way.",
        )
        return redirect("accounts:verify-notice")


class VerifyEmailView(TemplateView):
    template_name = "accounts/verify.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("form", VerificationNoticeForm())
        context.setdefault("verified", False)
        return context

    def get(self, request, uidb64, token, *args, **kwargs):
        user = _user_from_uid(uidb64)
        if _token_is_valid(user, token):
            if not user.is_email_verified:
                from django.utils import timezone

                user.email_verified_at = timezone.now()
                user.save(update_fields=("email_verified_at",))
            messages.success(request, "Your email address is verified.")
            if request.user.is_authenticated:
                return redirect("accounts:dashboard")
            return redirect("accounts:login")
        context = self.get_context_data(**kwargs)
        context.update({"verified": False, "verification_failed": True})
        return self.render_to_response(context, status=200)


class PasswordResetRequestView(FormView):
    template_name = "accounts/password_reset.html"
    form_class = PasswordResetRequestForm
    success_url = reverse_lazy("accounts:password-reset-done")

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST":
            try:
                _enforce_auth_rate_limits(
                    request,
                    route="accounts.password-reset",
                    limit=PASSWORD_RESET_RATE_LIMIT,
                    window_seconds=AUTHENTICATION_RATE_WINDOW,
                    email=request.POST.get("email", ""),
                )
            except RateLimitExceeded as error:
                return _rate_limited_response(
                    request,
                    self.get_form(),
                    self.template_name,
                    error,
                )
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        try:
            form.save(request=self.request)
        except Exception:
            pass
        messages.info(
            self.request,
            (
                "If an active account matches that email, a password-reset link "
                "is on its way."
            ),
        )
        return redirect(self.success_url)


class PasswordResetDoneView(TemplateView):
    template_name = "accounts/password_reset_done.html"


class PasswordResetConfirmView(FormView):
    template_name = "accounts/password_reset_confirm.html"
    form_class = PasswordResetConfirmForm
    success_url = reverse_lazy("accounts:password-reset-complete")

    def dispatch(self, request, *args, **kwargs):
        self.reset_user = _user_from_uid(kwargs.get("uidb64", ""))
        if request.method == "POST":
            try:
                _enforce_auth_rate_limits(
                    request,
                    route="accounts.password-reset-confirm",
                    limit=PASSWORD_RESET_RATE_LIMIT,
                    window_seconds=AUTHENTICATION_RATE_WINDOW,
                    email=self.reset_user.email if self.reset_user else "",
                )
            except RateLimitExceeded as error:
                return _rate_limited_response(
                    request,
                    self.get_form(),
                    self.template_name,
                    error,
                )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.reset_user or User(email="")
        return kwargs

    def _link_is_valid(self) -> bool:
        return bool(
            self.reset_user
            and self.reset_user.is_active
            and _token_is_valid(
                self.reset_user,
                self.kwargs.get("token", ""),
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["valid_link"] = self._link_is_valid()
        return context

    def form_valid(self, form):
        if not self._link_is_valid():
            form.add_error(None, "This password-reset link is invalid or has expired.")
            return self.form_invalid(form)
        self.reset_user.set_password(form.cleaned_data["new_password1"])
        self.reset_user.save(update_fields=("password",))
        self.request.session.cycle_key()
        if (
            self.request.user.is_authenticated
            and self.request.user.pk == self.reset_user.pk
        ):
            update_session_auth_hash(self.request, self.reset_user)
            self.request.session.cycle_key()
        messages.success(self.request, "Your password has been changed.")
        return redirect(self.success_url)

    def get(self, request, uidb64, token, *args, **kwargs):
        context = self.get_context_data(**kwargs)
        if not context["valid_link"]:
            context["form"] = self.get_form()
        return self.render_to_response(context, status=200)


class PasswordResetCompleteView(TemplateView):
    template_name = "accounts/password_reset_complete.html"


class ProfileView(LoginRequiredMixin, UpdateView):
    template_name = "accounts/profile.html"
    form_class = ProfileForm
    success_url = reverse_lazy("accounts:profile")

    def get_queryset(self):
        return Profile.objects.filter(user=self.request.user)

    def get_object(self, queryset=None):
        if self.request.method == "POST":
            return Profile.create_for_user(self.request.user)
        profile = Profile.objects.filter(user=self.request.user).first()
        return profile or Profile(user=self.request.user)

    def form_valid(self, form):
        profile = self.get_object()
        old_logo_key = profile.logo.name if profile.logo else ""
        try:
            response = super().form_valid(form)
        except Exception:
            new_logo_key = form.instance.logo.name if form.instance.logo else ""
            cleanup_failed = False
            if new_logo_key and new_logo_key != old_logo_key:
                from quotes.services.storage import delete_object

                try:
                    delete_object(new_logo_key)
                except Exception:
                    cleanup_failed = True
            if cleanup_failed:
                form.add_error(
                    None,
                    "The profile could not be saved, and logo cleanup requires retry.",
                )
            else:
                form.add_error(
                    None,
                    "The profile could not be saved. Please try again.",
                )
            response = self.form_invalid(form)
            response.status_code = 503
            return response
        new_logo_key = self.object.logo.name if self.object.logo else ""
        if old_logo_key and old_logo_key != new_logo_key:
            from quotes.services.storage import MediaCleanupError, delete_object

            try:
                delete_object(old_logo_key)
            except MediaCleanupError:
                messages.warning(
                    self.request,
                    "The profile was saved, but the previous logo needs "
                    "operator cleanup.",
                )
        record_authenticated_activity(self.request.user)
        messages.success(self.request, "Your profile settings have been saved.")
        return response


class SecurityView(LoginRequiredMixin, FormView):
    template_name = "accounts/security.html"
    form_class = SecurityForm
    success_url = reverse_lazy("accounts:security")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        user = form.save()
        update_session_auth_hash(self.request, user)
        self.request.session.cycle_key()
        try:
            send_security_notice_email(user=user)
        except Exception:
            pass
        record_authenticated_activity(user)
        messages.success(self.request, "Your password has been changed.")
        return redirect(self.success_url)


class DeleteAccountView(LoginRequiredMixin, FormView):
    template_name = "accounts/delete.html"
    form_class = DeleteAccountForm
    success_url = reverse_lazy("core:home")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        user = self.request.user
        try:
            request_account_deletion(
                user=user,
                source="owner",
                now=timezone.now(),
            )
        except (ValidationError, ProtectedError, ValueError) as error:
            form.add_error(None, error)
            return self.form_invalid(form)
        auth_logout(self.request)
        messages.info(
            self.request,
            "Your account has been deleted. Permanent deletion is scheduled "
            "after the recovery window.",
        )
        return redirect(self.success_url)


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "quotes/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        quotes = Quote.objects.filter(owner=self.request.user)
        status_counts = {status: 0 for status in QuoteStatus.values}
        rows = quotes.values("status").annotate(total=Count("id"))
        for row in rows:
            status_counts[row["status"]] = row["total"]
        status_summary = [
            {"value": value, "label": label, "count": status_counts[value]}
            for value, label in QuoteStatus.choices
        ]
        context.update(
            {
                "quote_count": quotes.count(),
                "recent_quotes": quotes.order_by("-updated_at")[:DASHBOARD_QUOTE_LIMIT],
                "status_counts": status_counts,
                "status_summary": status_summary,
                "status_choices": QuoteStatus.choices,
                "can_publish": self.request.user.can_publish,
            }
        )
        return context


class QuoteListView(LoginRequiredMixin, TemplateView):
    template_name = "quotes/list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        data = self.request.GET.copy()
        if not data.get("q") and data.get("search"):
            data["q"] = data["search"]
        form = QuoteFilterForm(data or None)
        quotes = Quote.objects.filter(owner=self.request.user)
        if form.is_valid():
            search = form.cleaned_data["q"].strip()
            status = form.cleaned_data["status"]
            if search:
                quotes = quotes.filter(
                    Q(project_title__icontains=search)
                    | Q(project_description__icontains=search)
                    | Q(client_name__icontains=search)
                    | Q(client_company__icontains=search)
                    | Q(client_email__icontains=search)
                    | Q(quote_number__icontains=search)
                )
            if status:
                quotes = quotes.filter(status=status)
        context.update(
            {
                "form": form,
                "quotes": quotes.order_by("-updated_at")[:100],
                "quote_count": quotes.count(),
            }
        )
        return context


VerificationView = VerifyEmailView
EmailVerificationView = VerifyEmailView
PasswordResetView = PasswordResetRequestView
ResetPasswordConfirmView = PasswordResetConfirmView
ProfileSettingsView = ProfileView
AccountSecurityView = SecurityView
AccountDeletionView = DeleteAccountView
DeleteView = DeleteAccountView
QuoteHistoryView = QuoteListView

login = LoginView.as_view()
signup = SignupView.as_view()
logout = LogoutView.as_view()
verify_notice = VerificationNoticeView.as_view()
verify = VerifyEmailView.as_view()
password_reset = PasswordResetRequestView.as_view()
password_reset_done = PasswordResetDoneView.as_view()
password_reset_confirm = PasswordResetConfirmView.as_view()
password_reset_complete = PasswordResetCompleteView.as_view()
profile = ProfileView.as_view()
security = SecurityView.as_view()
delete = DeleteAccountView.as_view()
dashboard = DashboardView.as_view()
quote_list = QuoteListView.as_view()
