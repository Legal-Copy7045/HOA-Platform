from django.conf import settings
from django.db import models
from django.utils import timezone


class TaskStatus(models.TextChoices):
    OPEN = "open", "Open"
    DONE = "done", "Done"
    DROPPED = "dropped", "Dropped"


class BoardTask(models.Model):
    """Follow-ups for the board and manager (not visible to owners)."""

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    assignee = models.ForeignKey(
        "community.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=TaskStatus.choices, default=TaskStatus.OPEN)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["status", "due_date", "id"]

    def __str__(self) -> str:
        return self.title

    def delete(self, *args, **kwargs):
        raise PermissionError("Tasks are closed or dropped, not deleted.")
