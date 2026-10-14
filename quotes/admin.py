from django.contrib import admin

from quotes.models import (
    Quote,
    QuoteEvent,
    QuoteLineItem,
    QuoteNumberSequence,
    QuoteVersion,
    ShareLink,
)


@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    list_display = (
        "quote_number",
        "owner",
        "status",
        "project_title",
        "chosen_total",
        "currency",
        "created_at",
    )
    list_filter = ("status", "currency")
    search_fields = (
        "quote_number",
        "project_title",
        "client_name",
        "client_email",
        "owner__email",
    )
    list_select_related = ("owner",)
    readonly_fields = (
        "owner",
        "quote_number",
        "deposit_amount",
        "realized_hourly_equivalent",
        "current_published_version",
        "created_at",
        "updated_at",
    )


@admin.register(QuoteLineItem)
class QuoteLineItemAdmin(admin.ModelAdmin):
    list_display = ("quote", "label", "amount", "sort_order")
    list_filter = ("quote__status",)
    search_fields = ("quote__quote_number", "label")
    list_select_related = ("quote",)


@admin.register(QuoteNumberSequence)
class QuoteNumberSequenceAdmin(admin.ModelAdmin):
    list_display = ("year", "last_allocated_number", "updated_at")
    ordering = ("-year",)
    readonly_fields = ("year", "last_allocated_number", "updated_at")


@admin.register(QuoteVersion)
class QuoteVersionAdmin(admin.ModelAdmin):
    list_display = ("quote", "version_number", "frozen_total", "currency", "created_at")
    list_filter = ("currency", "reason")
    search_fields = ("quote__quote_number", "quote__project_title")
    list_select_related = ("quote",)
    readonly_fields = (
        "quote",
        "version_number",
        "snapshot_schema_version",
        "snapshot",
        "frozen_total",
        "currency",
        "logo_object_key",
        "reason",
        "created_at",
    )

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ShareLink)
class ShareLinkAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "quote_version",
        "state",
        "expires_at",
        "revoked_at",
        "created_at",
    )
    list_filter = ("encryption_key_version",)
    search_fields = ("token_hash", "quote_version__quote__quote_number")
    list_select_related = ("quote_version", "quote_version__quote")
    readonly_fields = (
        "quote_version",
        "token_hash",
        "encrypted_token",
        "encryption_key_version",
        "associated_data",
        "expires_at",
        "revoked_at",
        "first_viewed_at",
        "created_by",
        "created_at",
    )


@admin.register(QuoteEvent)
class QuoteEventAdmin(admin.ModelAdmin):
    list_display = ("quote", "event_type", "event_time", "share_link")
    list_filter = ("event_type",)
    search_fields = ("quote__quote_number", "note")
    list_select_related = ("quote", "share_link", "quote_version")
    readonly_fields = (
        "quote",
        "share_link",
        "quote_version",
        "quote_version_id_snapshot",
        "event_type",
        "note",
        "response_idempotency_key",
        "payload_fingerprint",
        "event_time",
        "user_agent_fingerprint",
    )

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
