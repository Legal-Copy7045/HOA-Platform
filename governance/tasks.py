from procrastinate.contrib.django import app


@app.task(queue="default")
def flush_secret_choices(kind: str, target_id: int) -> int:
    """After a secret ballot, election ballot or anonymous survey response
    commits: write pooled choices once enough wait (D56). Separate
    transaction from every cast, by construction."""
    from governance import mixing

    return mixing.flush(kind, int(target_id))


@app.periodic(cron="19 * * * *")  # hourly
@app.task(queue="maintenance", queueing_lock="secret_choice_sweep")
def sweep_secret_choices(timestamp: int) -> int:
    """Safety net: flush anything the after-commit task missed, and
    everything for votes, elections and surveys that can take no more."""
    from governance import mixing

    return mixing.sweep()


@app.periodic(cron="*/5 * * * *")  # every five minutes
@app.task(queue="maintenance", queueing_lock="close_due_votes")
def close_due_votes(timestamp: int) -> int:
    """Close every vote, election and survey whose deadline has passed,
    through the normal close services as SYSTEM (D61): pooled secret
    choices are written and the results become final."""
    from governance import window

    return window.close_due()


@app.periodic(cron="*/15 * * * *")  # every fifteen minutes
@app.task(queue="maintenance", queueing_lock="vote_deadline_reminders")
def vote_deadline_reminders(timestamp: int) -> int:
    """About 48 hours before a formal vote or an election closes, remind the
    owners of homes that have not voted, once (D61)."""
    from governance import window

    return window.send_reminders()
