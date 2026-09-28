from django.contrib import admin

from .models import (
    Defect,
    Inspection,
    InspectionFinding,
    InspectionResponse,
    InspectionTemplate,
    LaborEntry,
    MaintenanceAlert,
    MaintenancePlan,
    MaintenanceRequest,
    MaintenanceTrigger,
    ServicePackage,
    WorkOrder,
    WorkOrderTask,
)


@admin.register(ServicePackage, InspectionTemplate)
class VersionedTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "version", "organization", "active", "created_at")
    list_filter = ("active", "organization")
    search_fields = ("name",)
    readonly_fields = ("created_at", "updated_at")


class TriggerInline(admin.TabularInline):
    model = MaintenanceTrigger
    extra = 0


@admin.register(MaintenancePlan)
class MaintenancePlanAdmin(admin.ModelAdmin):
    list_display = ("name", "asset", "due_status", "active", "last_calculated_at")
    list_filter = ("due_status", "active", "organization")
    search_fields = ("name", "asset__unit_number")
    inlines = (TriggerInline,)


class InspectionResponseInline(admin.TabularInline):
    model = InspectionResponse
    extra = 0
    can_delete = False


@admin.register(Inspection)
class InspectionAdmin(admin.ModelAdmin):
    list_display = ("asset", "template", "status", "performed_by", "started_at", "submitted_at")
    list_filter = ("status", "organization")
    search_fields = ("asset__unit_number", "performed_by__username")
    inlines = (InspectionResponseInline,)


@admin.register(InspectionFinding)
class InspectionFindingAdmin(admin.ModelAdmin):
    list_display = (
        "asset",
        "description",
        "severity",
        "safety_related",
        "status",
        "created_at",
    )
    list_filter = ("status", "severity", "safety_related", "organization")
    search_fields = ("asset__unit_number", "description", "response__question")
    readonly_fields = (
        "organization",
        "inspection",
        "response",
        "asset",
        "reported_by",
        "status",
        "severity",
        "safety_related",
        "description",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request: object) -> bool:
        return False

    def has_delete_permission(self, request: object, obj: object | None = None) -> bool:
        return False


@admin.register(Defect)
class DefectAdmin(admin.ModelAdmin):
    list_display = ("asset", "category", "severity", "safety_related", "status", "created_at")
    list_filter = ("status", "severity", "safety_related", "organization")
    search_fields = ("asset__unit_number", "description")


@admin.register(MaintenanceRequest)
class MaintenanceRequestAdmin(admin.ModelAdmin):
    list_display = ("summary", "asset", "priority", "status", "created_at")
    list_filter = ("status", "priority", "organization")
    search_fields = ("summary", "asset__unit_number")


class TaskInline(admin.TabularInline):
    model = WorkOrderTask
    extra = 0


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ("number", "asset", "summary", "priority", "status", "assigned_to")
    list_filter = ("status", "priority", "organization")
    search_fields = ("number", "summary", "asset__unit_number")
    inlines = (TaskInline,)


@admin.register(MaintenanceAlert)
class MaintenanceAlertAdmin(admin.ModelAdmin):
    list_display = ("title", "asset", "severity", "status", "occurrence_count", "last_seen_at")
    list_filter = ("status", "severity", "organization")
    search_fields = ("title", "dedupe_key", "asset__unit_number")


@admin.register(LaborEntry)
class LaborEntryAdmin(admin.ModelAdmin):
    list_display = ("work_order", "technician", "minutes", "cost", "created_at")
    search_fields = ("work_order__number", "technician__username")
    readonly_fields = (
        "organization",
        "work_order",
        "technician",
        "started_at",
        "ended_at",
        "minutes",
        "hourly_rate",
        "cost",
        "note",
        "corrects",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request: object) -> bool:
        return False

    def has_delete_permission(self, request: object, obj: object | None = None) -> bool:
        return False
