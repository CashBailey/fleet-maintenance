from django.contrib import admin

from .models import (
    Asset,
    AssetStatusEvent,
    AssetType,
    Component,
    ComponentInstallation,
    Meter,
    MeterReading,
)


@admin.register(AssetType)
class AssetTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "organization")
    list_filter = ("organization", "category")
    search_fields = ("name",)


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("unit_number", "asset_type", "status", "home_location", "organization")
    list_filter = ("organization", "status", "asset_type")
    search_fields = ("unit_number", "vin", "serial_number", "make", "model")
    readonly_fields = ("status", "status_changed_at", "archived_at", "created_at", "updated_at")


@admin.register(Meter)
class MeterAdmin(admin.ModelAdmin):
    list_display = ("asset", "name", "kind", "unit", "active")
    list_filter = ("organization", "kind", "active")
    search_fields = ("asset__unit_number", "name")


@admin.register(AssetStatusEvent)
class AssetStatusEventAdmin(admin.ModelAdmin):
    list_display = ("asset", "previous_status", "new_status", "actor", "occurred_at")
    list_filter = ("organization", "new_status", "classification")
    readonly_fields = [field.name for field in AssetStatusEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MeterReading)
class MeterReadingAdmin(admin.ModelAdmin):
    list_display = ("meter", "value", "quality", "source", "observed_at")
    list_filter = ("organization", "quality", "source")
    search_fields = ("meter__asset__unit_number", "external_id")
    readonly_fields = [field.name for field in MeterReading._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Component)
class ComponentAdmin(admin.ModelAdmin):
    list_display = ("serial_number", "kind", "manufacturer", "model", "organization")
    list_filter = ("organization", "kind")
    search_fields = ("serial_number", "manufacturer", "model")


@admin.register(ComponentInstallation)
class ComponentInstallationAdmin(admin.ModelAdmin):
    list_display = ("component", "asset", "installed_at", "removed_at", "source")
    list_filter = ("organization", "source")
    search_fields = ("component__serial_number", "asset__unit_number")
    readonly_fields = [field.name for field in ComponentInstallation._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
