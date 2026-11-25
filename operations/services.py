from __future__ import annotations

from django.utils import timezone

from audit.services import record_event
from core.policy import require
from operations.models import BoardTask, TaskStatus


class OperationsError(ValueError):
    pass


def create_task(
    *, title: str, description: str = "", assignee=None, due_date=None, actor, request=None
) -> BoardTask:
    require(actor, "task.manage")
    if not title.strip():
        raise OperationsError("A task needs a title.")
    task = BoardTask.objects.create(
        title=title.strip()[:200],
        description=description,
        assignee=assignee,
        due_date=due_date,
        created_by=actor,
    )
    record_event("task.created", actor=actor, request=request, target=task)
    return task


def close_task(task: BoardTask, *, status: str, actor, request=None) -> BoardTask:
    require(actor, "task.manage")
    if status not in (TaskStatus.DONE, TaskStatus.DROPPED) or task.status != TaskStatus.OPEN:
        raise OperationsError("Only open tasks can be closed.")
    task.status, task.closed_at = status, timezone.now()
    task.save(update_fields=["status", "closed_at"])
    record_event(
        "task.closed", actor=actor, request=request, target=task, metadata={"status": status}
    )
    return task
