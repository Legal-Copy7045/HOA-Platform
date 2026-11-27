"""D6x, step 2 of 3: pooled secret choices are sealed, never stored readable.

Rows already waiting in the pool (an open secret vote, election or anonymous
survey during the upgrade) are sealed here: each is deleted and inserted
again, sealed, with a new id and sort key, in this migration's transaction,
so the new row no longer even shares the casting transaction's id with the
participation. The next migration drops the readable ``payload`` column.
Uses the pure functions in ``governance.sealing`` (format 1)."""

import json
import uuid

from django.db import migrations
from django.db.models import Count

import governance.models

TEXT_ANSWER_MAX = 1000  # governance.models.TEXT_ANSWER_MAX when written


def _target(row):
    if row.vote_id:
        return "vote", row.vote_id
    if row.election_id:
        return "election", row.election_id
    return "survey", row.survey_id


def _size(apps, kind, target_id):
    from governance import sealing

    if kind == "vote":
        return sealing.vote_bound()
    if kind == "election":
        election = apps.get_model("governance", "Election").objects.get(pk=target_id)
        return sealing.election_bound(election.seats)
    questions = list(
        apps.get_model("governance", "SurveyQuestion")
        .objects.filter(survey_id=target_id)
        .annotate(n=Count("choices"))
        .values_list("kind", "n")
    )
    return sealing.survey_bound(questions, TEXT_ANSWER_MAX)


def _decode(payload):
    if isinstance(payload, (bytes, str)):
        return json.loads(payload)
    return payload


def seal_existing(apps, schema_editor):
    from governance import sealing

    Pending = apps.get_model("governance", "PendingSecretChoice")
    for row in list(Pending.objects.filter(sealed__isnull=True)):
        kind, target_id = _target(row)
        sealed = sealing.seal(kind, target_id, _decode(row.payload), _size(apps, kind, target_id))
        fields = {
            "vote_id": row.vote_id,
            "election_id": row.election_id,
            "survey_id": row.survey_id,
        }
        row.delete()
        Pending.objects.create(
            id=uuid.uuid4(), sort_key=governance.models._mix_key(), sealed=sealed, **fields
        )


def unseal_existing(apps, schema_editor):
    from governance import sealing

    Pending = apps.get_model("governance", "PendingSecretChoice")
    for row in list(Pending.objects.filter(payload__isnull=True)):
        kind, target_id = _target(row)
        payload = sealing.unseal(kind, target_id, row.sealed)
        fields = {
            "vote_id": row.vote_id,
            "election_id": row.election_id,
            "survey_id": row.survey_id,
        }
        row.delete()
        Pending.objects.create(
            id=uuid.uuid4(), sort_key=governance.models._mix_key(), payload=payload, **fields
        )


class Migration(migrations.Migration):
    dependencies = [
        ("governance", "0018_pending_secret_choice_sealed"),
    ]

    operations = [
        migrations.RunPython(seal_existing, unseal_existing),
    ]
