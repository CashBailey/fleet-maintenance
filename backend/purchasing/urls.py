from django.urls import path

from . import views

urlpatterns = [
    path("vendors/", views.vendors, name="vendors"),
    path("purchase-requests/", views.purchase_requests, name="purchase-requests"),
    path(
        "purchase-requests/<uuid:purchase_request_id>/transition/",
        views.purchase_request_transition,
        name="purchase-request-transition",
    ),
    path("purchase-orders/", views.purchase_orders, name="purchase-orders"),
    path(
        "purchase-orders/<uuid:purchase_order_id>/transition/",
        views.purchase_order_transition,
        name="purchase-order-transition",
    ),
    path("receipts/", views.receipts, name="receipts"),
    path("receipts/<uuid:receipt_id>/reverse/", views.reverse_receipt, name="reverse-receipt"),
]
