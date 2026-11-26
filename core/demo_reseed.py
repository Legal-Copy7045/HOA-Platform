"""Settings -> Demo data: reseeding a demo or test portal (decision D53).

Three layers refuse it anywhere else:

1. the page answers 404 and its Settings entry is absent unless
   :func:`allowed` (SEED_DEMO_ALLOWED and DEPLOYMENT_STAGE poc or local);
2. ``host_tasks.request_task`` refuses a ``reseed_demo`` task unless
   :func:`allowed`, the dataset is in the registry and RESEED was typed;
3. the VM agent checks the dataset name again ([a-z_], and in the registry)
   and ``infra/poc/reseed-demo.sh`` refuses unless the VM's .env says
   DEPLOYMENT_STAGE=poc and SEED_DEMO_ALLOWED=true.

The web process never touches the database schema or the host: it only
inserts the ``core.HostTask`` row. Production settings force
SEED_DEMO_ALLOWED off outside DEPLOYMENT_STAGE=poc.
"""

from __future__ import annotations

from django.conf import settings

from community import demo
from core.host_tasks import RESEED_WORD, HostTaskRefused

# Stages where demo data may be (re)loaded from the portal.
STAGES = ("poc", "local")


def allowed() -> bool:
    return bool(settings.SEED_DEMO_ALLOWED) and settings.DEPLOYMENT_STAGE in STAGES


def check_request(dataset: str, confirmation: str) -> None:
    """Raise HostTaskRefused unless a reseed may be requested as given."""
    if not allowed():
        raise HostTaskRefused("Demo data cannot be loaded on this portal.")
    try:
        demo.get(dataset)
    except KeyError:
        raise HostTaskRefused("Choose one of the demo datasets listed.") from None
    if (confirmation or "").strip() != RESEED_WORD:
        raise HostTaskRefused(f"Type {RESEED_WORD} to confirm.")


def last_seed():
    from core.models import DemoSeed

    return DemoSeed.objects.first()


def recent_reseeds(limit: int = 10):
    from core.models import HostTask

    return HostTask.objects.filter(kind=HostTask.Kind.RESEED_DEMO).select_related("requested_by")[
        :limit
    ]
