import pytest
from django.db import IntegrityError

from accounts.models import User


@pytest.mark.django_db
def test_email_is_normalised_and_unique_case_insensitively():
    user = User.objects.create_user("  Owner07@Example.COM ", "a-long-enough-password")
    assert user.email == "owner07@example.com"
    with pytest.raises(IntegrityError):
        User.objects.create_user("OWNER07@example.com")


@pytest.mark.django_db
def test_user_without_password_cannot_log_in_with_one():
    user = User.objects.create_user("invitee@example.com")
    assert not user.has_usable_password()


@pytest.mark.django_db
def test_users_are_never_deleted():
    user = User.objects.create_user("keep@example.com")
    with pytest.raises(PermissionError):
        user.delete()


def test_str_does_not_expose_email():
    assert "example.com" not in str(User(pk=5, email="x@example.com"))
