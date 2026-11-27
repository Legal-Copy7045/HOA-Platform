from django.urls import path, re_path

from operations import finance_views as fv
from operations import onboarding_views as ov
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
    # Vendor onboarding (D79). The public form: the only pages besides the
    # sign-in steps that open without signing in.
    path("vendor-form/start/<str:token>/", ov.start, name="vendor_onboarding_start"),
    path("vendor-form/", ov.form, name="vendor_onboarding_form"),
    path("vendor-form/upload/", ov.upload, name="vendor_onboarding_upload"),
    path("vendor-form/submit/", ov.submit, name="vendor_onboarding_submit"),
    path("vendor-form/done/", ov.done, name="vendor_onboarding_done"),
    path("finance/vendors/onboarding/", ov.onboarding_list, name="vendor_onboarding_list"),
    path("finance/vendors/onboarding/invite/", ov.invite, name="vendor_onboarding_invite"),
    path("finance/vendors/onboarding/<int:pk>/", ov.review, name="vendor_onboarding_review"),
    re_path(
        r"^finance/vendors/onboarding/(?P<pk>[0-9]+)/do/(?P<action>[a-z_]+)/$",
        ov.action,
        name="vendor_onboarding_action",
    ),
    path(
        "finance/vendors/onboarding/<int:pk>/files/<int:file_id>/",
        ov.file,
        name="vendor_onboarding_file",
    ),
    path("finance/vendors/<int:pk>/onboarding/", ov.vendor_page, name="vendor_onboarding_vendor"),
    re_path(
        r"^finance/vendors/(?P<pk>[0-9]+)/onboarding/(?P<action>update|complete)/$",
        ov.vendor_action,
        name="vendor_onboarding_vendor_action",
    ),
    path("finance/vendors/<int:pk>/file.pdf", ov.vendor_file_summary, name="vendor_file_summary"),
    path("finance/vendors/requirements/", ov.requirements, name="vendor_requirements"),
    path("finance/vendors/w9-retention/", ov.w9_retention, name="vendor_w9_retention"),
    re_path(
        r"^finance/vendors/w9-retention/(?P<pk>[0-9]+)/(?P<step>propose|confirm)/$",
        ov.w9_disposal,
        name="vendor_w9_disposal",
    ),
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
