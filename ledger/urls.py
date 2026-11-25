from django.urls import path, re_path

from ledger import views

urlpatterns = [
    path("budget/", views.owner_budget, name="owner_budget"),
    path("my-account/", views.my_account, name="my_account"),
    path("my-account/<int:lot_id>/statement.pdf", views.my_statement_pdf, name="my_statement_pdf"),
    path("finance/ledger/", views.ledger_home, name="ledger_home"),
    path("finance/ledger/lot/<int:pk>/", views.lot_ledger, name="lot_ledger"),
    re_path(
        r"^finance/ledger/lot/(?P<pk>[0-9]+)/do/(?P<action>[a-z]+)/$",
        views.lot_ledger_action,
        name="lot_ledger_action",
    ),
    re_path(
        r"^finance/ledger/lot/(?P<pk>[0-9]+)/(?P<kind>statement|reminder)\.pdf$",
        views.lot_statement_pdf,
        name="lot_statement_pdf",
    ),
    path("finance/collections/", views.collections, name="collections"),
    path("finance/collections/<int:pk>/", views.collection_detail, name="collection_detail"),
    re_path(
        r"^finance/collections/(?P<pk>[0-9]+)/do/(?P<action>[a-z_]+)/$",
        views.collection_action,
        name="collection_action",
    ),
    path("finance/budgets/", views.budgets, name="budgets"),
    path("finance/budgets/<int:pk>/", views.budget_detail, name="budget_detail"),
    path("resale/", views.resales, name="resales"),
    path("resale/<int:pk>/", views.resale_detail, name="resale_detail"),
    re_path(
        r"^resale/(?P<pk>[0-9]+)/do/(?P<action>[a-z_]+)/$",
        views.resale_action,
        name="resale_action",
    ),
    path("resale/<int:pk>/certificate.pdf", views.resale_pdf, name="resale_pdf"),
]
