from django.urls import path

from reports import views

urlpatterns = [
    path("reports/", views.index, name="reports"),
    path("reports/<slug:key>/", views.run, name="report_run"),
]
