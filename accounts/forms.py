from __future__ import annotations

from django import forms
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordChangeForm,
    PasswordResetForm,
    SetPasswordForm,
    UserCreationForm,
)
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.urls import reverse
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode

from accounts.models import Profile
from accounts.services import canonicalize_email
from quotes.models import QuoteStatus

User = get_user_model()


def _account_url(request, route: str, user, token: str) -> str:
    path = reverse(
        route,
        kwargs={
            "uidb64": force_str(urlsafe_base64_encode(force_bytes(user.pk))),
            "token": token,
        },
    )
    if request is None:
        return path
    return request.build_absolute_uri(path)


def send_verification_email(*, user, request=None) -> None:
    token = default_token_generator.make_token(user)
    url = _account_url(request, "accounts:verify", user, token)
    subject = "Verify your Scopequote email"
    body = (
        "Confirm your Scopequote email address to publish private client links.\n\n"
        f"{url}\n\n"
        "If you did not create this account, you can ignore this message."
    )
    message = EmailMultiAlternatives(
        subject,
        body,
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
    )
    message.send(fail_silently=False)


def send_password_reset_email(*, user, request=None) -> None:
    token = default_token_generator.make_token(user)
    url = _account_url(request, "accounts:password-reset-confirm", user, token)
    subject = "Reset your Scopequote password"
    body = (
        "Use the secure link below to choose a new Scopequote password.\n\n"
        f"{url}\n\n"
        "If you did not request this, you can ignore this message."
    )
    message = EmailMultiAlternatives(
        subject,
        body,
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
    )
    message.send(fail_silently=False)


def send_security_notice_email(*, user) -> None:
    message = EmailMultiAlternatives(
        "Your Scopequote password was changed",
        (
            "Your Scopequote password was changed successfully. "
            "If this was not you, contact support immediately."
        ),
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
    )
    message.send(fail_silently=True)


class EmailAuthenticationForm(AuthenticationForm):
    email = forms.EmailField(
        label="Email",
        max_length=254,
        widget=forms.EmailInput(
            attrs={
                "id": "id_email",
                "autocomplete": "email",
                "autofocus": True,
            }
        ),
    )

    def __init__(self, request=None, *args, **kwargs):
        super().__init__(request, *args, **kwargs)
        self.fields.pop("username", None)
        self.fields["email"].widget.attrs["maxlength"] = 254

    def clean_email(self):
        return canonicalize_email(self.cleaned_data["email"])

    def clean(self):
        email = self.cleaned_data.get("email")
        password = self.cleaned_data.get("password")
        if email is not None and password:
            self.user_cache = authenticate(
                self.request,
                username=email,
                password=password,
            )
            if self.user_cache is None:
                raise self.get_invalid_login_error()
            self.confirm_login_allowed(self.user_cache)
        return self.cleaned_data

    def get_invalid_login_error(self):
        return ValidationError(
            "Please enter a correct email address and password.",
            code="invalid_login",
        )

    def confirm_login_allowed(self, user):
        if not user.is_active:
            raise self.get_invalid_login_error()


class SignupForm(UserCreationForm):
    email = forms.EmailField(
        label="Email",
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("email",)
        field_classes = {"email": forms.EmailField}

    def clean_email(self):
        email = canonicalize_email(self.cleaned_data["email"])
        self.duplicate_email = User.objects.filter(email__iexact=email).exists()
        return email

    def validate_unique(self):
        return

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = canonicalize_email(user.email)
        if commit:
            user.save()
        return user


class VerificationNoticeForm(forms.Form):
    email = forms.EmailField(
        label="Email",
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )

    def clean_email(self):
        return canonicalize_email(self.cleaned_data["email"])


class PasswordResetRequestForm(PasswordResetForm):
    email = forms.EmailField(
        label="Email",
        max_length=254,
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )

    def clean_email(self):
        return canonicalize_email(self.cleaned_data["email"])

    def get_users(self, email):
        users = User.objects.filter(email__iexact=email, is_active=True)
        return (user for user in users if user.has_usable_password())

    def save(self, request=None, **kwargs):
        for user in self.get_users(self.cleaned_data["email"]):
            try:
                send_password_reset_email(user=user, request=request)
            except Exception:
                continue
        return self.cleaned_data["email"]


class PasswordResetConfirmForm(SetPasswordForm):
    pass


class ProfileForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = (
            "full_name",
            "business_name",
            "phone",
            "website",
            "default_currency",
            "default_deposit_percentage",
            "default_quote_validity_days",
            "default_accent_color",
            "logo",
        )
        widgets = {
            "full_name": forms.TextInput(attrs={"autocomplete": "name"}),
            "business_name": forms.TextInput(attrs={"autocomplete": "organization"}),
            "phone": forms.TextInput(attrs={"autocomplete": "tel"}),
            "website": forms.URLInput(attrs={"autocomplete": "url"}),
            "logo": forms.ClearableFileInput(),
        }

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_logo(self):
        upload = self.cleaned_data.get("logo")
        if not upload:
            return upload
        owner = self.user or getattr(self.instance, "user", None)
        if owner is None or not owner.pk:
            raise ValidationError("A valid account is required for a logo upload.")
        from quotes.services.storage import sanitized_logo_upload

        return sanitized_logo_upload(upload, owner_id=owner.pk)

    def clean_default_deposit_percentage(self):
        return self.cleaned_data["default_deposit_percentage"]


class SecurityForm(PasswordChangeForm):
    pass


class DeleteAccountForm(forms.Form):
    password = forms.CharField(
        label="Current password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )
    confirm = forms.BooleanField(
        label=(
            "I understand my account will be permanently deleted after the "
            "recovery window."
        ),
        error_messages={"required": "Confirm that you understand this action."},
    )

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_password(self):
        password = self.cleaned_data["password"]
        if self.user is None or not self.user.check_password(password):
            raise ValidationError(
                "Enter your current password to continue.",
                code="invalid_password",
            )
        return password


class QuoteFilterForm(forms.Form):
    q = forms.CharField(
        label="Search quotes",
        max_length=160,
        required=False,
        widget=forms.SearchInput(
            attrs={"placeholder": "Project, client, or quote number"}
        ),
    )
    status = forms.ChoiceField(
        label="Status",
        required=False,
        choices=(("", "All statuses"), *QuoteStatus.choices),
    )


LoginForm = EmailAuthenticationForm
EmailVerificationForm = VerificationNoticeForm
EmailVerificationNoticeForm = VerificationNoticeForm
VerificationForm = VerificationNoticeForm
PasswordResetForm = PasswordResetRequestForm
SetPasswordForm = PasswordResetConfirmForm
AccountDeletionForm = DeleteAccountForm
SecuritySettingsForm = SecurityForm
