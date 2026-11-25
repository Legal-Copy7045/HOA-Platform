from core.exports import exporter


@exporter("ledger")
def _ledger(user):
    from community.models import Ownership
    from core.policy import roles_for
    from core.scoping import system_access
    from ledger.services import owner_statement

    out = []
    for lot_id in sorted(roles_for(user).owned_lot_ids):
        with system_access("export: own ownership start"):
            start = (
                Ownership.objects.filter(person__user=user, lot_id=lot_id)
                .active()
                .order_by("start_date")
                .values_list("start_date", flat=True)
                .first()
            )
        st = owner_statement(lot_id, start)
        out.append(
            {
                "lot_id": lot_id,
                "brought_forward_cents": st.brought_forward,
                "balance_cents": st.balance,
                "entries": [
                    {
                        "date": e.posted_on.isoformat(),
                        "description": e.description,
                        "amount_cents": e.amount_cents,
                    }
                    for e in st.entries
                ],
            }
        )
    return out
