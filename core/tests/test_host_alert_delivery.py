"""host_alert exits non-zero when the alert reached nobody, so the VM's timers
keep the report and send it again (R2-006)."""

import io
from unittest import mock

import pytest
from django.core import mail
from django.core.management import CommandError, call_command

from audit.models import AuditEvent
from community.models import Role
from community.tests.factories import make_member

pytestmark = pytest.mark.django_db


def _alert(stdin="changed: /etc/sudoers\n"):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("sys.stdin", io.StringIO(stdin))
        call_command("host_alert", "--kind", "integrity", stdout=io.StringIO())


@pytest.fixture
def locmem(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


def test_succeeds_when_at_least_one_sysadmin_was_sent_it(locmem):
    make_member(roles=[Role.SYSADMIN])
    _alert()
    assert len(mail.outbox) == 1
    assert AuditEvent.objects.get(action="system.host_alert").metadata["sent"] == 1


def test_fails_when_there_is_no_sysadmin_email(locmem):
    make_member(board=True)  # not a sysadmin
    with pytest.raises(CommandError, match="reached nobody"):
        _alert()
    assert not mail.outbox
    assert AuditEvent.objects.get(action="system.host_alert").metadata["sent"] == 0


def test_fails_when_every_send_fails(locmem):
    make_member(roles=[Role.SYSADMIN])
    make_member(roles=[Role.SYSADMIN])
    with (
        mock.patch(
            "core.management.commands.host_alert.send_mail", side_effect=OSError("mail down")
        ),
        pytest.raises(CommandError, match="reached nobody"),
    ):
        _alert()
    assert AuditEvent.objects.get(action="system.host_alert").metadata["sent"] == 0


def test_counts_only_the_sends_that_went_out(locmem):
    make_member(roles=[Role.SYSADMIN])
    make_member(roles=[Role.SYSADMIN])
    real = mail.send_mail
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("mail down")
        return real(*args, **kwargs)

    with mock.patch("core.management.commands.host_alert.send_mail", side_effect=flaky):
        _alert()
    assert len(mail.outbox) == 1
    assert AuditEvent.objects.get(action="system.host_alert").metadata["sent"] == 1


def test_manage_py_exit_code_is_non_zero_without_recipients(locmem):
    with pytest.raises(SystemExit) as exit_info, pytest.MonkeyPatch.context() as mp:
        mp.setattr("sys.stdin", io.StringIO("x\n"))
        from django.core.management import ManagementUtility

        ManagementUtility(["manage.py", "host_alert", "--kind", "falco"]).execute()
    assert exit_info.value.code == 1
