import pytest
from django.core.exceptions import PermissionDenied

from community.models import Role
from community.tests.factories import LotFactory, make_member
from operations import services
from operations.models import TaskStatus

pytestmark = pytest.mark.django_db


def test_board_and_manager_manage_tasks():
    task = services.create_task(title="Get pond quotes", actor=make_member(board=True))
    services.close_task(task, status=TaskStatus.DONE, actor=make_member(roles=[Role.MANAGER]))
    task.refresh_from_db()
    assert task.status == TaskStatus.DONE and task.closed_at
    with pytest.raises(services.OperationsError):
        services.close_task(task, status=TaskStatus.DONE, actor=make_member(board=True))


def test_owners_cannot_see_or_manage_tasks():
    with pytest.raises(PermissionDenied):
        services.create_task(title="x", actor=make_member(lots=[LotFactory()]))
