from django.urls import path, re_path

from operations import views

urlpatterns = [
    path("board/tasks/", views.tasks, name="tasks"),
    re_path(
        r"^board/tasks/(?P<pk>[0-9]+)/(?P<status>done|dropped)/$",
        views.task_close,
        name="task_close",
    ),
]
