"""Re-audit of review A1: an open invitation created before the fix could
point at an address other than the person's own. Revoke any such invitation;
the board can invite again (to the address on file)."""

from django.db import migrations
from django.utils import timezone


def revoke(apps, schema_editor):
    Invitation = apps.get_model("accounts", "Invitation")
    now = timezone.now()
    for invitation in Invitation.objects.filter(
        accepted_at__isnull=True, revoked_at__isnull=True
    ).select_related("person"):
        on_file = (invitation.person.email or "").strip().lower()
        if invitation.email.strip().lower() != on_file:
            Invitation.objects.filter(pk=invitation.pk).update(  # nosec B106 (clears a hash)
                revoked_at=now, token_hash=""
            )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_mfa_reset"),
        ("community", "0005_contact_profile"),  # pinned (a moving "latest" broke history)
    ]
    operations = [migrations.RunPython(revoke, migrations.RunPython.noop)]
