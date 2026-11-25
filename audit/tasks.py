from procrastinate.contrib.django import app

from audit.services import purge_expired_ip_addresses


@app.periodic(cron="17 3 * * *")  # daily, 03:17 server time
@app.task(queue="maintenance", queueing_lock="purge_expired_ip_addresses")
def purge_expired_ips(timestamp: int) -> int:
    return purge_expired_ip_addresses()
