from django.urls import path

from . import document_library, views

urlpatterns = [
    path("auth/csrf/", views.csrf_cookie, name="csrf"),
    path("auth/login/", views.login_view, name="login"),
    path("auth/logout/", views.logout_view, name="logout"),
    path("auth/me/", views.me_view, name="me"),
    path("users/", views.users, name="users"),
    path("users/<uuid:user_id>/mfa/", views.provision_user_mfa, name="provision-user-mfa"),
    path("api-tokens/", views.api_tokens, name="api-tokens"),
    path(
        "api-tokens/<uuid:token_id>/revoke/",
        views.revoke_api_token,
        name="api-token-revoke",
    ),
    path("roles/", views.roles, name="roles"),
    path("locations/", views.locations, name="locations"),
    path("bootstrap/", views.bootstrap, name="bootstrap"),
    path("search/", views.global_search, name="search"),
    path("reports/operations/", views.operations_report, name="operations-report"),
    path("audit-events/", views.audit_events, name="audit-events"),
    path("attachments/", views.attachments, name="attachments"),
    path(
        "attachments/<uuid:attachment_id>/download/",
        views.attachment_download,
        name="attachment-download",
    ),
    path("documents/", document_library.documents, name="documents"),
    path("documents/search/", document_library.document_search, name="document-search"),
    path(
        "documents/<uuid:document_id>/approve/",
        document_library.approve_document,
        name="document-approve",
    ),
    path(
        "documents/<uuid:document_id>/download/",
        document_library.document_download,
        name="document-download",
    ),
    path("comments/", views.comments, name="comments"),
    path("notifications/", views.notifications, name="notifications"),
    path("webhooks/", views.webhooks, name="webhooks"),
    path(
        "webhooks/<uuid:webhook_id>/rotate-secret/",
        views.rotate_webhook_secret,
        name="webhook-rotate-secret",
    ),
    path(
        "webhooks/<uuid:webhook_id>/status/",
        views.set_webhook_status,
        name="webhook-status",
    ),
    path("webhooks/deliveries/", views.webhook_deliveries, name="webhook-deliveries"),
    path(
        "webhooks/deliveries/<uuid:delivery_id>/retry/",
        views.retry_webhook_delivery,
        name="webhook-delivery-retry",
    ),
    path("offline/sync/", views.offline_sync, name="offline-sync"),
    path("import/assets/", views.import_assets, name="import-assets"),
    path("export/", views.export_data, name="export"),
    path(
        "users/<uuid:user_id>/offline-access/revoke/",
        views.revoke_user_offline_access,
        name="revoke-user-offline-access",
    ),
    path("users/<uuid:user_id>/disable/", views.disable_user, name="disable-user"),
]
