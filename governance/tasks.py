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
