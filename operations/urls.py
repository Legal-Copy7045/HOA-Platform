from django.urls import path, re_path

from operations import finance_views as fv
from operations import request_views as rv
from operations import views

urlpatterns = [
    path("board/tasks/", views.tasks, name="tasks"),
    path("board/compliance/", views.compliance, name="compliance"),
    re_path(
        r"^board/tasks/(?P<pk>[0-9]+)/(?P<status>done|dropped)/$",
        views.task_close,
        name="task_close",
    ),
    path("requests/", rv.requests_list, name="requests"),
    path("requests/<int:pk>/", rv.request_detail, name="request_detail"),
    path("requests/<int:pk>/photos/", rv.request_attach, name="request_attach"),
    path("requests/<int:pk>/photos/<int:file_id>/", rv.request_file, name="request_file"),
    re_path(
        r"^requests/(?P<pk>[0-9]+)/do/(?P<action>[a-z_]+)/$",
        rv.request_action,
        name="request_action",
    ),
    path("board/requests/<int:pk>/assign/", rv.request_assign, name="request_assign"),
    path("board/requests/<int:pk>/work-order/", rv.request_work_order, name="request_work_order"),
    path("finance/vendors/", fv.vendors, name="vendors"),
    path("finance/vendors/<int:pk>/", fv.vendor_detail, name="vendor_detail"),
    path("finance/work-orders/", fv.work_orders, name="work_orders"),
    path("finance/work-orders/<int:pk>/", fv.work_order_detail, name="work_order_detail"),
    re_path(
        r"^finance/work-orders/(?P<pk>[0-9]+)/do/(?P<action>[a-z_]+)/$",
        fv.work_order_action,
        name="work_order_action",
    ),
    path("finance/contracts/", fv.contracts, name="contracts"),
    path("finance/contracts/<int:pk>/", fv.contract_detail, name="contract_detail"),
    re_path(
        r"^finance/contracts/(?P<pk>[0-9]+)/do/(?P<action>[a-z_]+)/$",
        fv.contract_action,
        name="contract_action",
    ),
    path("finance/invoices/", fv.invoices, name="invoices"),
    path("finance/invoices/<int:pk>/", fv.invoice_detail, name="invoice_detail"),
    re_path(
        r"^finance/invoices/(?P<pk>[0-9]+)/do/(?P<action>[a-z_]+)/$",
        fv.invoice_action,
        name="invoice_action",
    ),
    path("finance/invoices/<int:pk>/files/<int:file_id>/", fv.invoice_file, name="invoice_file"),
]

# Finance options (D83 capital projects, D85 insurance claims), each an
# optional feature (core.features).
from operations import finance_option_views as fov  # noqa: E402

urlpatterns += [
    path("maintenance/projects/", fov.capital_projects, name="capital_projects"),
    path(
        "maintenance/projects/<int:pk>/", fov.capital_project_detail, name="capital_project_detail"
    ),
    path(
        "maintenance/projects/<int:pk>/files/<int:file_id>/",
        fov.capital_project_file,
        name="capital_project_file",
    ),
    path("projects/", fov.community_projects, name="community_projects"),
    path("maintenance/insurance-claims/", fov.insurance_claims, name="insurance_claims"),
    path(
        "maintenance/insurance-claims/<int:pk>/",
        fov.insurance_claim_detail,
        name="insurance_claim_detail",
    ),
    path(
        "maintenance/insurance-claims/<int:pk>/files/<int:file_id>/",
        fov.insurance_claim_file,
        name="insurance_claim_file",
    ),
]
