from django.urls import path

from accounts import views

urlpatterns = [
    path("invite/accept/", views.invite_accept, name="invite_accept"),
    path("invite/<str:token>/", views.invite_landing, name="invite_landing"),
]
