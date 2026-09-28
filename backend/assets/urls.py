from django.urls import path

from . import views

urlpatterns = [
    path("", views.assets_collection, name="assets"),
    path(
        "external/<str:source_system>/<str:external_id>/",
        views.external_asset_detail,
        name="asset-external",
    ),
    path(
        "external/<str:source_system>/<str:external_id>/meters/",
        views.external_asset_meter_readings,
        name="asset-external-meters",
    ),
    path("<uuid:asset_id>/", views.asset_detail, name="asset-detail"),
    path("<uuid:asset_id>/history/", views.asset_history, name="asset-history"),
    path("<uuid:asset_id>/meters/", views.meter_readings, name="meter-readings"),
    path("meter-readings/<uuid:reading_id>/correct/", views.correct_meter, name="correct-meter"),
    path("<uuid:asset_id>/availability/", views.change_availability, name="asset-availability"),
    path("<uuid:asset_id>/components/", views.asset_components, name="asset-components"),
    path("components/<uuid:component_id>/", views.component_detail, name="component-detail"),
    path(
        "components/<uuid:component_id>/remove/",
        views.remove_installed_component,
        name="remove-component",
    ),
]
