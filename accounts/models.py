"""Login accounts.

A ``User`` is only a login. Community identity (name, ownerships, seats,
roles) lives on ``community.Person``, linked one-to-one (decision D1). Users
are never deleted; deactivate them instead, so audit history stays intact.
"""

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from core.scoping import ScopedModel, ScopedQuerySet


def normalize_email_address(email: str) -> str:
    return (email or "").strip().lower()


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def create_user(self, email: str, password: str | None = None, **extra) -> "User":
        if not email:
            raise ValueError("An email address is required")
        user = self.model(email=normalize_email_address(email), **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str | None = None, **extra) -> "User":
        """Local development only: Django admin is disabled in production (D9)."""
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(max_length=254, unique=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("email"), name="accounts_user_email_ci_unique"),
        ]

    def __str__(self) -> str:
        return f"User #{self.pk}"

    def save(self, *args, **kwargs) -> None:
        self.email = normalize_email_address(self.email)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Users are never deleted; set is_active=False instead.")


class InvitationQuerySet(ScopedQuerySet):
    def pending(self):
        now = timezone.now()
        return self.filter(accepted_at__isnull=True, revoked_at__isnull=True, expires_at__gt=now)


class Invitation(ScopedModel):
    """An invitation for an existing Person to create their login (D4: grants nothing).

    The link token is generated when the email is sent; only its SHA-256 hash
    is stored. Re-sending generates a new token, invalidating the old link.
    """

    person = models.ForeignKey(
        "community.Person", on_delete=models.PROTECT, related_name="invitations"
    )
    email = models.EmailField(max_length=254)
    token_hash = models.CharField(max_length=64, blank=True, db_index=True)
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    sent_at = models.DateTimeField(null=True, blank=True)
    send_count = models.PositiveSmallIntegerField(default=0)
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    objects = InvitationQuerySet.as_manager()
    scope_person_path = "person"

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"Invitation #{self.pk}"

    @property
    def status(self) -> str:
        if self.accepted_at:
            return "accepted"
        if self.revoked_at:
            return "revoked"
        if self.expires_at <= timezone.now():
            return "expired"
        return "pending" if self.sent_at else "queued"

    def delete(self, *args, **kwargs):
        raise PermissionError("Invitations are revoked, never deleted.")
