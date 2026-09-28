from django.urls import path

from . import views

urlpatterns = [
    path("parts/", views.parts_collection, name="parts"),
    path("parts/<uuid:part_id>/history/", views.part_history, name="part-history"),
    path("warehouses/", views.warehouses, name="warehouses"),
    path("bins/", views.bins, name="bins"),
    path("bins/<uuid:bin_id>/history/", views.bin_history, name="bin-history"),
    path("stock/", views.stock, name="stock"),
    path("reservations/", views.reservations, name="reservations"),
    path("issues/", views.issues, name="issues"),
    path("returns/", views.returns, name="returns"),
    path("adjustments/", views.adjustments, name="adjustments"),
    path("counts/", views.counts, name="counts"),
    path("counts/<uuid:count_id>/approve/", views.approve_count, name="approve-count"),
]
