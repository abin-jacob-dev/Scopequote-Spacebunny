from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from accounts.models import Profile, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2"),
            },
        ),
    )
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name")}),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    list_display = ("email", "is_staff", "is_active")
    ordering = ("email",)
    search_fields = ("email",)


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "business_name",
        "default_currency",
        "default_deposit_percentage",
        "default_quote_validity_days",
    )
    list_select_related = ("user",)
    search_fields = ("user__email", "full_name", "business_name")
