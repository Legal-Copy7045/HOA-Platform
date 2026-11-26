"""Login accounts.

A ``User`` is only a login. Community identity (name, ownerships, seats,
roles) lives on ``community.Person``, linked one-to-one (decision D1). Users
are never deleted; deactivate them instead, so audit history stays intact.
"""

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from core.append_only import AppendOnlyModel
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
    # Bumping this signs the user out of every session (assisted reset, D5).
    session_generation = models.PositiveIntegerField(default=0)

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

    def _get_session_auth_hash(self, secret=None):
        # Django checks this on every request; any change ends existing sessions.
        from django.utils.crypto import salted_hmac

        return salted_hmac(
            "accounts.User.session",
            f"{self.password}:{self.session_generation}",
            secret=secret,
            algorithm="sha256",
        ).hexdigest()


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


class VerificationMethod(models.TextChoices):
    IN_PERSON = "in_person", "In person"
    PHONE_KNOWN_NUMBER = "phone_known_number", "Phone call to a number on file"
    VIDEO_CALL = "video_call", "Video call"
    OTHER = "other", "Other (explain to the approving board member)"


class MfaReset(ScopedModel):
    """Assisted reset after losing every authenticator (decision D5).

    Board member A starts it, a different board member B approves it. Approval
    removes the authenticators, signs the user out everywhere and produces a
    single-use enrollment code that B hands over out of band.
    """

    MAX_CODE_ATTEMPTS = 5

    subject = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    verification_method = models.CharField(max_length=24, choices=VerificationMethod.choices)
    started_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    started_at = models.DateTimeField(default=timezone.now)
    request_expires_at = models.DateTimeField()
    approved_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    code_hash = models.CharField(max_length=64, blank=True)
    code_expires_at = models.DateTimeField(null=True, blank=True)
    code_used_at = models.DateTimeField(null=True, blank=True)
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    objects = ScopedQuerySet.as_manager()
    scope_person_path = "subject__person"

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self) -> str:
        return f"MFA reset #{self.pk}"

    @property
    def status(self) -> str:
        now = timezone.now()
        if self.cancelled_at:
            return "cancelled"
        if self.code_used_at:
            return "completed"
        if self.approved_at:
            if self.failed_attempts >= self.MAX_CODE_ATTEMPTS:
                return "code_locked"
            return "awaiting_code" if self.code_expires_at > now else "code_expired"
        return "awaiting_approval" if self.request_expires_at > now else "expired"

    def delete(self, *args, **kwargs):
        raise PermissionError("Resets are cancelled, never deleted.")


class AccessReview(AppendOnlyModel):
    """A board member confirmed they reviewed every login and its access
    (quarterly access review, Milestone 4). Counts only, no names.
    Append-only: a later review is a new row."""

    reviewed_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    reviewed_at = models.DateTimeField(default=timezone.now)
    logins = models.PositiveIntegerField(default=0)
    flagged = models.PositiveIntegerField(default=0)
    inactive = models.PositiveIntegerField(default=0, help_text="No sign-in for 90+ days.")
    without_mfa = models.PositiveIntegerField(default=0)
    sysadmins = models.PositiveIntegerField(default=0)
    managers = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-reviewed_at", "-id"]

    def __str__(self) -> str:
        return f"Access review #{self.pk}"


class RecoveryCancelledBy(models.TextChoices):
    PERSON = "person", "The person, on the recovery page"
    BOARD = "board", "A board member"
    SIGNED_IN = "signed_in", "The person signed in with their passkey or app"


class AccountRecovery(ScopedModel):
    """Self-service recovery of a lost authenticator, with a waiting period (D58).

    Only for ordinary logins (owners, account contacts, tenants, former
    owners); staff roles use the two-board-member reset (MfaReset). The wait
    and the post-recovery lock in effect when the request starts are stored
    on it, so a later change of the board's settings applies to new requests
    only. Never deleted: cancelled or completed.
    """

    subject = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    started_at = models.DateTimeField(default=timezone.now)
    wait_hours = models.PositiveSmallIntegerField()
    lock_hours = models.PositiveSmallIntegerField()
    ready_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    cancel_reason = models.CharField(max_length=12, choices=RecoveryCancelledBy.choices, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    objects = ScopedQuerySet.as_manager()
    scope_person_path = "subject__person"

    class Meta:
        ordering = ["-started_at", "-id"]
        indexes = [models.Index(fields=["subject", "started_at"])]

    def __str__(self) -> str:
        return f"Account recovery #{self.pk}"

    @property
    def status(self) -> str:
        now = timezone.now()
        if self.cancelled_at:
            return "cancelled"
        if self.completed_at:
            return "completed"
        if now < self.ready_at:
            return "waiting"
        return "ready" if now < self.expires_at else "expired"

    @property
    def is_open(self) -> bool:
        return self.status in ("waiting", "ready")

    @property
    def lock_until(self):
        if not self.completed_at:
            return None
        from datetime import timedelta

        return self.completed_at + timedelta(hours=self.lock_hours)

    def delete(self, *args, **kwargs):
        raise PermissionError("Recoveries are cancelled, never deleted.")


class RecoveryLinkPurpose(models.TextChoices):
    START = "start", "Start a recovery"
    FINISH = "finish", "Finish a recovery"


class RecoveryLink(ScopedModel):
    """A single-use emailed link for account recovery. Only the SHA-256 of the
    token is stored; the token itself exists only in the email."""

    subject = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    purpose = models.CharField(max_length=8, choices=RecoveryLinkPurpose.choices)
    recovery = models.ForeignKey(
        AccountRecovery, null=True, blank=True, on_delete=models.PROTECT, related_name="links"
    )
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    objects = ScopedQuerySet.as_manager()
    scope_person_path = "subject__person"

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"Recovery link #{self.pk}"

    @property
    def usable(self) -> bool:
        return self.used_at is None and self.expires_at > timezone.now()

    def delete(self, *args, **kwargs):
        raise PermissionError("Recovery links expire, never deleted.")
