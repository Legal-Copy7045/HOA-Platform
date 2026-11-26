"""'Download my data': this person's own event answers and volunteer sign-ups."""

from core.exports import exporter


@exporter("events")
def _events(user):
    from comms.models import EventRsvp, VolunteerSignup
    from core.policy import roles_for

    person_id = roles_for(user).person_id
    if person_id is None:
        return {}
    answers = EventRsvp.objects.for_user(user).filter(person_id=person_id).select_related("event")
    signups = (
        VolunteerSignup.objects.for_user(user)
        .filter(person_id=person_id)
        .select_related("slot__event")
    )
    return {
        "rsvps": [
            {
                "event": r.event.title,
                "starts_at": r.event.starts_at.isoformat(),
                "answer": r.answer,
                "guests": r.guests,
            }
            for r in answers
        ],
        "volunteer_slots": [{"event": s.slot.event.title, "slot": s.slot.title} for s in signups],
    }
