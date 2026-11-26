from django.urls import path

from payments import views

urlpatterns = [
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
]
