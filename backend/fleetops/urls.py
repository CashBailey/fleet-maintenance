from core.views import health, serve_spa, service_worker
from django.urls import include, path, re_path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path("health/live", health, {"ready": False}, name="health-live"),
    path("health/ready", health, {"ready": True}, name="health-ready"),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="api-docs"),
    path("api/v1/", include("core.urls")),
    path("api/v1/assets/", include("assets.urls")),
    path("api/v1/maintenance/", include("maintenance.urls")),
    path("api/v1/inventory/", include("inventory.urls")),
    path("api/v1/purchasing/", include("purchasing.urls")),
    path("api/v1/integrations/", include("integrations.urls")),
    path("sw.js", service_worker, name="service-worker"),
    path("manifest.webmanifest", service_worker, {"manifest": True}, name="web-manifest"),
    re_path(r"^(?!api/|admin/|health/|static/|media/).*$", serve_spa, name="spa"),
]
