from django.contrib import admin

from .models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseRequest,
    Receipt,
    ReceiptLine,
    Vendor,
    VendorPart,
)


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "organization", "active")
    list_filter = ("active", "organization")
    search_fields = ("code", "name")


@admin.register(VendorPart)
class VendorPartAdmin(admin.ModelAdmin):
    list_display = ("vendor", "vendor_part_number", "part", "unit_cost", "active")
    list_filter = ("active", "preferred", "organization")
    search_fields = ("vendor_part_number", "part__number", "part__name")


class PurchaseOrderLineInline(admin.TabularInline):
    model = PurchaseOrderLine
    extra = 0


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ("number", "vendor", "status", "created_by", "created_at")
    list_filter = ("status", "organization", "emergency")
    search_fields = ("number", "vendor__code", "vendor__name")
    inlines = (PurchaseOrderLineInline,)


@admin.register(PurchaseRequest)
class PurchaseRequestAdmin(admin.ModelAdmin):
    list_display = ("part", "quantity", "status", "requested_by", "created_at")
    list_filter = ("status", "organization")
    search_fields = ("part__number", "part__name", "reason")


class ReceiptLineInline(admin.TabularInline):
    model = ReceiptLine
    extra = 0
    can_delete = False
    readonly_fields = (
        "organization",
        "purchase_order_line",
        "part",
        "bin",
        "quantity",
        "unit_cost",
        "stock_transaction",
        "reversal_of",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request: object, obj: object | None = None) -> bool:
        return False


@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    list_display = ("number", "purchase_order", "status", "received_by", "received_at")
    list_filter = ("status", "organization")
    search_fields = ("number", "purchase_order__number", "packing_slip")
    readonly_fields = (
        "organization",
        "purchase_order",
        "number",
        "status",
        "operation_id",
        "received_by",
        "received_at",
        "packing_slip",
        "reason",
        "reversal_of",
        "reversed_by",
        "reversed_at",
        "created_at",
        "updated_at",
    )
    inlines = (ReceiptLineInline,)

    def has_add_permission(self, request: object) -> bool:
        return False

    def has_delete_permission(self, request: object, obj: object | None = None) -> bool:
        return False
