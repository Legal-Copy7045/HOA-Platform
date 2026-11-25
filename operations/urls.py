from django.urls import path, re_path

from operations import finance_views as fv
from operations import request_views as rv
from operations import views

urlpatterns = [
    path("board/tasks/", views.tasks, name="tasks"),
    re_path(
        r"^board/tasks/(?P<pk>[0-9]+)/(?P<status>done|dropped)/$",
        views.task_close,
        name="task_close",
    ),
    path("requests/", rv.requests_list, name="requests"),
    path("requests/<int:pk>/", rv.request_detail, name="request_detail"),
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
    path("finance/invoices/", fv.invoices, name="invoices"),
    path("finance/invoices/<int:pk>/", fv.invoice_detail, name="invoice_detail"),
    re_path(
        r"^finance/invoices/(?P<pk>[0-9]+)/do/(?P<action>[a-z_]+)/$",
        fv.invoice_action,
        name="invoice_action",
    ),
    path("finance/invoices/<int:pk>/files/<int:file_id>/", fv.invoice_file, name="invoice_file"),
]
