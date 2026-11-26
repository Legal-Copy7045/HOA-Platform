from django.urls import path

from payments import views

urlpatterns = [
    path("my-account/pay/", views.pay_start, name="pay_start"),
    path("my-account/<int:lot_id>/pay/", views.pay_online, name="pay_online"),
    path("my-account/<int:lot_id>/pay/checkout/", views.pay_checkout, name="pay_checkout"),
    path("my-account/payments/<int:pk>/return/", views.pay_return, name="pay_return"),
    path("my-account/payments/<int:pk>/receipt.pdf", views.payment_receipt, name="payment_receipt"),
    path("finance/online-payments/", views.online_payments, name="online_payments"),
    path(
        "finance/online-payments/items/<int:pk>/done/",
        views.payment_alert_resolve,
        name="payment_alert_resolve",
    ),
    # Automatic payments (D46)
    path("my-account/<int:lot_id>/autopay/", views.autopay, name="autopay"),
    path("my-account/<int:lot_id>/autopay/start/", views.autopay_start, name="autopay_start"),
    path("my-account/<int:lot_id>/autopay/cancel/", views.autopay_cancel, name="autopay_cancel"),
    path("my-account/autopay/<int:pk>/return/", views.autopay_return, name="autopay_return"),
    # D47: schedule (monthly / once a year) and the board's review
    path(
        "my-account/<int:lot_id>/autopay/schedule/",
        views.autopay_schedule,
        name="autopay_schedule",
    ),
    path(
        "finance/online-payments/autopay-reviews/<int:pk>/",
        views.autopay_review,
        name="autopay_review",
    ),
]
