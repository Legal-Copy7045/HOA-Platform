from django.urls import path

from comms import views

urlpatterns = [
    path("announcements/", views.announcements, name="announcements"),
    path("announcements/<int:pk>/", views.announcement_detail, name="announcement_detail"),
    path("calendar/", views.calendar, name="calendar"),
    path("calendar/<int:pk>/cancel/", views.event_cancel, name="event_cancel"),
]
