from allauth.mfa.totp.internal.auth import (
    format_hotp_value,
    hotp_value,
    yield_hotp_counters_from_time,
)


def current_totp(secret: str) -> str:
    counter = next(iter(yield_hotp_counters_from_time()))
    return format_hotp_value(hotp_value(secret, counter))


PASSWORD = "correct horse battery staple 42"
