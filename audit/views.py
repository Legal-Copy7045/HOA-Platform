from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render

from audit.models import AuditEvent
from core import policy


@login_required
def sysadmin_activity(request):
    """Everything sysadmins did, plus every change to who is a sysadmin (D9)."""
    policy.require(request.user, "audit.view_sysadmin_activity")
    events = (
        AuditEvent.objects.filter(
            Q(actor_is_sysadmin=True)
            | Q(action__startswith="role.sysadmin")
            | Q(action__in=["role.granted", "role.ended"], metadata__role="sysadmin")
            | Q(action="sysadmin.breakglass")
            | Q(channel="cli")
        )
        .select_related("actor")
        .order_by("-occurred_at", "-id")[:500]
    )
    return render(request, "audit/sysadmin_activity.html", {"events": events})
