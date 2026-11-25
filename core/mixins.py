"""View building blocks that make the authorization rules hard to skip.

    class AllLotsView(PolicyRequiredMixin, ListView):
        permission = "lot.view_all"
        model = Lot

    class LotDetailView(ScopedObjectMixin, DetailView):
        model = Lot

* PolicyRequiredMixin: login required, then core.policy check -> 403.
* ScopedObjectMixin: every query goes through Model.objects.for_user(), so an
  object outside the user's scope is a 404 - indistinguishable from one that
  does not exist (no existence leak). Lookups by primary key are therefore
  always scoped.
"""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ImproperlyConfigured

from core import policy


class PolicyRequiredMixin(LoginRequiredMixin):
    permission: str | None = None

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not self.permission:
            raise ImproperlyConfigured(f"{type(self).__name__} must set `permission`")
        policy.require(request.user, self.permission)
        return super().dispatch(request, *args, **kwargs)


class ScopedObjectMixin(LoginRequiredMixin):
    def get_queryset(self):
        model = getattr(self, "model", None)
        if model is None:
            raise ImproperlyConfigured(f"{type(self).__name__} must set `model`")
        return model.objects.for_user(self.request.user)
