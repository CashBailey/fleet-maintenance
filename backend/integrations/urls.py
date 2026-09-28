from django.urls import path

from . import views

urlpatterns = [
    path("devices/", views.devices, name="devices"),
    path("devices/<uuid:device_id>/associate/", views.associate_device, name="associate-device"),
    path(
        "devices/<uuid:device_id>/rotate-token/",
        views.rotate_device_token,
        name="rotate-device-token",
    ),
    path(
        "devices/<uuid:device_id>/status/",
        views.set_device_status,
        name="set-device-status",
    ),
    path("telematics/autopi/v1/messages/", views.autopi_ingest, name="autopi-ingest"),
    path("data-quality/", views.data_quality, name="data-quality"),
]
