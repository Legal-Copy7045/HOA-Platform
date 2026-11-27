from django.urls import path

from ledger.books import views

urlpatterns = [
    path("finance/close/", views.books_close, name="books_close"),
    path("finance/close/do/", views.books_action, name="books_action"),
    path("finance/accounting/books/accounts/", views.books_accounts, name="books_accounts"),
    path("finance/accounting/books/categories/", views.books_coding, name="books_coding"),
    path(
        "finance/accounting/quickbooks.csv",
        views.accounting_quickbooks,
        name="accounting_quickbooks",
    ),
    path("finance/statements/", views.financial_statements, name="financial_statements"),
    path(
        "finance/statements/statements.csv",
        views.financial_statements_csv,
        name="financial_statements_csv",
    ),
    path(
        "finance/statements/statements.pdf",
        views.financial_statements_pdf,
        name="financial_statements_pdf",
    ),
    path("finance/statements/package/", views.year_end_package, name="year_end_package"),
    path("finance/tax/", views.tax_worksheet, name="tax_worksheet"),
    path("finance/tax/download/", views.tax_download, name="tax_download"),
]
