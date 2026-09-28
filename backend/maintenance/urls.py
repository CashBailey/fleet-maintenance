from django.urls import path

from . import views

urlpatterns = [
    path("service-packages/", views.service_packages, name="service-packages"),
    path("plans/", views.maintenance_plans, name="maintenance-plans"),
    path("plans/recalculate/", views.recalculate_plans, name="recalculate-plans"),
    path(
        "plans/<uuid:plan_id>/create-work-order/",
        views.plan_to_work_order,
        name="plan-to-work-order",
    ),
    path("inspection-templates/", views.inspection_templates, name="inspection-templates"),
    path("inspections/", views.inspections, name="inspections"),
    path("inspections/<uuid:inspection_id>/", views.inspection_detail, name="inspection-detail"),
    path("inspections/<uuid:inspection_id>/void/", views.void_inspection, name="void-inspection"),
    path("defects/", views.defects, name="defects"),
    path("defects/<uuid:defect_id>/transition/", views.defect_transition, name="defect-transition"),
    path("defects/<uuid:defect_id>/request/", views.defect_to_request, name="defect-to-request"),
    path("requests/", views.requests_collection, name="requests"),
    path(
        "requests/<uuid:request_id>/transition/",
        views.request_transition,
        name="request-transition",
    ),
    path(
        "requests/<uuid:request_id>/work-order/",
        views.request_to_work_order,
        name="request-to-work-order",
    ),
    path("work-orders/", views.work_orders, name="work-orders"),
    path("personnel/external/", views.external_employees, name="external-employees"),
    path(
        "personnel/external/<str:source_system>/<str:external_employee_id>/",
        views.external_employee_detail,
        name="external-employee-detail",
    ),
    path("work-orders/<uuid:work_order_id>/", views.work_order_detail, name="work-order-detail"),
    path(
        "work-orders/<uuid:work_order_id>/assignments/",
        views.work_order_assignments,
        name="work-order-assignments",
    ),
    path(
        "work-orders/<uuid:work_order_id>/transition/",
        views.work_order_transition,
        name="work-order-transition",
    ),
    path(
        "work-orders/<uuid:work_order_id>/tasks/", views.work_order_tasks, name="work-order-tasks"
    ),
    path(
        "work-orders/<uuid:work_order_id>/tasks/<uuid:task_id>/",
        views.work_order_task,
        name="work-order-task",
    ),
    path("work-orders/<uuid:work_order_id>/labor/", views.labor_entries, name="labor-entries"),
    path("alerts/", views.maintenance_alerts, name="maintenance-alerts"),
    path(
        "alerts/<uuid:alert_id>/transition/",
        views.maintenance_alert_transition,
        name="maintenance-alert-transition",
    ),
]
