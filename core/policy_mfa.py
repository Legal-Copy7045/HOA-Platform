"""Which people have MFA enrolled. Implemented in step 5 (django-allauth MFA).

Returns None while MFA does not exist yet, meaning "do not filter".
"""


def people_with_mfa() -> set[int] | None:
    return None
