from django.contrib import admin

from .models import (
    Bin,
    InventoryCount,
    InventoryCountLine,
    Part,
    PartCrossReference,
    Reservation,
    StockBalance,
    StockTransaction,
    Warehouse,
)

admin.site.register(Part)
admin.site.register(PartCrossReference)
admin.site.register(Warehouse)
admin.site.register(Bin)
admin.site.register(StockBalance)
admin.site.register(StockTransaction)
admin.site.register(Reservation)
admin.site.register(InventoryCount)
admin.site.register(InventoryCountLine)
