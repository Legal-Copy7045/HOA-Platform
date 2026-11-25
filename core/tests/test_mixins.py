import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.views.generic import DetailView, View

from community.models import Lot
from community.tests.factories import LotFactory, make_member
from core.mixins import PolicyRequiredMixin, ScopedObjectMixin

pytestmark = pytest.mark.django_db


class AllLots(PolicyRequiredMixin, View):
    permission = "lot.view_all"

    def get(self, request):
        return HttpResponse("ok")


class LotDetail(ScopedObjectMixin, DetailView):
    model = Lot

    def render_to_response(self, context, **kwargs):
        return HttpResponse(str(context["object"].pk))


def _call(view, rf, user, **kwargs):
    request = rf.get("/")
    request.user = user
    return view.as_view()(request, **kwargs)


def test_policy_mixin_redirects_anonymous_to_login(rf):
    assert _call(AllLots, rf, AnonymousUser()).status_code == 302


def test_policy_mixin_forbids_without_permission(rf):
    with pytest.raises(PermissionDenied):
        _call(AllLots, rf, make_member(lots=[LotFactory()]))


def test_policy_mixin_allows_with_permission(rf):
    assert _call(AllLots, rf, make_member(board=True)).status_code == 200


def test_scoped_detail_404s_for_someone_elses_lot(rf):
    mine, theirs = LotFactory(), LotFactory()
    owner = make_member(lots=[mine])
    assert _call(LotDetail, rf, owner, pk=mine.pk).content == str(mine.pk).encode()
    with pytest.raises(Http404):
        _call(LotDetail, rf, owner, pk=theirs.pk)


def test_policy_mixin_requires_a_permission_to_be_declared(rf):
    class Broken(PolicyRequiredMixin, View):
        pass

    from django.core.exceptions import ImproperlyConfigured

    with pytest.raises(ImproperlyConfigured):
        _call(Broken, rf, make_member(board=True))
