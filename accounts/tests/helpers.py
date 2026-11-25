import time

from allauth.mfa.totp.internal.auth import format_hotp_value, hotp_value


def totp_counter(offset: int = 0) -> int:
    """The current 30-second TOTP window, plus ``offset`` windows."""
    return int(time.time()) // 30 + offset


def totp_code(secret: str, offset: int = 0) -> str:
    return format_hotp_value(hotp_value(secret, totp_counter(offset)))


def current_totp(secret: str) -> str:
    return totp_code(secret)


PASSWORD = "correct horse battery staple 42"  # nosec B105 - test-only
