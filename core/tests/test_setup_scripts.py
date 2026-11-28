"""Guided one-time set-up scripts for secrets typed on the server (Teller,
Stripe), their read-only checks, and the plain-button styling fix."""

import base64
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command

POC = Path("infra/poc")


def test_teller_script_checks_the_files_and_never_takes_secrets_on_the_command_line():
    script = (POC / "set-teller.sh").read_text()
    assert "openssl x509" in script and "openssl pkey" in script
    assert "do not belong together" in script and "-checkend 0" in script
    assert 'TELLER_CERTIFICATE_B64 "$(base64 -w0 "$CERT")"' in script
    assert "BANK_FEED_TOKEN_KEY" in script and "openssl rand -hex 32" in script
    assert 'BACKUP="$(env_backup .env teller)"' in script
    # The check runs after the restart, and a failed check puts back only
    # Teller's keys (never the whole old .env).
    assert script.index("start-portal.sh") < script.index("manage.py check_teller")
    assert 'env_restore_keys .env "$BACKUP"' in script.split("check_teller", 1)[1]


def test_stripe_script_checks_the_key_after_saving_it():
    script = (POC / "set-stripe-key.sh").read_text()
    assert 'read -r -s -p "Stripe restricted key (hidden): "' in script
    assert script.index("start-portal.sh") < script.index("manage.py check_stripe")


@pytest.mark.parametrize(
    "name",
    [
        "set-stripe-key.sh",
        "set-teller.sh",
        "set-usps.sh",
        "set-bounce-mailbox.sh",
        "set-smtp2go.sh",
    ],
)
def test_secret_scripts_remove_the_old_env_copy_after_a_passed_check(name):
    """D87 (R2-007), 0.43.0: the .env.before-* copy (env_backup: mode 600)
    holds the superseded secret and is shredded once the check passes; a
    failed check puts back only the script's own keys, never the whole file."""
    script = (POC / name).read_text()
    assert "cp -p .env" not in script and "set_env " not in script
    assert 'BACKUP="$(env_backup .env ' in script
    after_check = script.split("manage.py check_", 1)[1]
    assert 'env_discard_backup "$BACKUP"' in after_check
    restore = script.index('env_restore_keys .env "$BACKUP"')
    assert restore < script.rindex('env_discard_backup "$BACKUP"')
    assert "cp .env.before" not in script


def test_stripe_script_refuses_a_full_secret_key():
    script = (POC / "set-stripe-key.sh").read_text()
    assert 'if [[ "$KEY" == sk_* ]]; then' in script
    assert "^rk_(test|live)_" in script


def test_usps_password_reaches_only_the_worker():
    compose = Path("docker-compose.yml").read_text()
    shared = compose.split("x-logging", 1)[0]
    assert "  USPS_IV_PASSWORD: " not in shared
    assert "USPS_IV_PASSWORD_SET: ${USPS_IV_PASSWORD:+true}" in shared
    worker = compose.split("\n  worker:\n", 1)[1].split("\nvolumes:", 1)[0]
    assert "USPS_IV_PASSWORD: ${USPS_IV_PASSWORD:-}" in worker
    assert "exec -T worker python manage.py check_usps_iv" in (POC / "set-usps.sh").read_text()


def test_check_teller_reports_what_is_missing(settings):
    settings.TELLER_APPLICATION_ID = ""
    settings.TELLER_ENVIRONMENT = "development"
    settings.TELLER_CERTIFICATE_B64 = ""
    settings.TELLER_PRIVATE_KEY_B64 = ""
    settings.BANK_FEED_TOKEN_KEY = ""  # nosec B105  # empty: "not set"
    with pytest.raises(CommandError) as err:
        call_command("check_teller")
    message = str(err.value)
    assert "application ID is missing" in message and "BANK_FEED_TOKEN_KEY" in message
    assert "certificate and key are missing" in message


def test_check_teller_refuses_a_file_that_is_not_pem(settings):
    settings.TELLER_APPLICATION_ID = "app_abcdef123"
    settings.TELLER_ENVIRONMENT = "development"
    settings.TELLER_CERTIFICATE_B64 = base64.b64encode(b"not a pem").decode()
    settings.TELLER_PRIVATE_KEY_B64 = base64.b64encode(b"not a pem").decode()
    settings.BANK_FEED_TOKEN_KEY = "k" * 64
    with pytest.raises(CommandError, match="not a PEM file"):
        call_command("check_teller")


def test_check_teller_sandbox_needs_no_certificate(settings, capsys):
    settings.TELLER_APPLICATION_ID = "app_abcdef123"
    settings.TELLER_ENVIRONMENT = "sandbox"
    settings.TELLER_CERTIFICATE_B64 = ""
    settings.TELLER_PRIVATE_KEY_B64 = ""
    settings.BANK_FEED_TOKEN_KEY = "k" * 64
    call_command("check_teller")
    assert "Teller set up (sandbox)" in capsys.readouterr().out


def test_check_stripe_reports_mode_and_missing_rights(monkeypatch, capsys):
    from payments import stripe_api

    monkeypatch.setattr(stripe_api, "secret_key", lambda: "rk_test_x")
    calls = []

    def fake_get(path, params=None, *, what="get"):
        calls.append(path)
        if path == "/v1/checkout/sessions" and len(calls) > 2:
            raise stripe_api.StripeError(403, "permission_error")
        return {"livemode": False}

    monkeypatch.setattr(stripe_api, "get", fake_get)
    call_command("check_stripe")
    assert "Stripe key OK (test mode)." in capsys.readouterr().out
    with pytest.raises(CommandError, match="lacks read rights for: checkout_sessions"):
        call_command("check_stripe")

    def refused(path, params=None, *, what="get"):
        raise stripe_api.StripeError(401, "authentication_error")

    monkeypatch.setattr(stripe_api, "get", refused)
    with pytest.raises(CommandError, match="refused the key"):
        call_command("check_stripe")


def test_check_stripe_refuses_a_full_secret_key_and_says_why(settings):
    """D87 (SEC-002): only a restricted key; the message names the fix, never the key."""
    fake = "sk_live_" + "Q" * 24  # synthetic, never a real key
    settings.PAYMENTS_PROVIDER = "stripe"
    settings.STRIPE_SECRET_KEY = fake
    settings.DEPLOYMENT_STAGE = "production"
    settings.PAYMENTS_LIVE_ALLOWED = True
    with pytest.raises(CommandError, match=r"restricted key \(rk_") as caught:
        call_command("check_stripe")
    assert fake not in str(caught.value)
    script = Path("infra/poc/set-stripe-key.sh").read_text()
    assert "sk_" in script and "restricted" in script


def test_plain_secondary_buttons_get_the_full_button_style():
    """The sign-in page's "Use my passkey instead" is type=button: it must
    get the padding and corners of every other button, not just colours."""
    css = Path("static/css/site.css").read_text()
    rule = next(
        line for line in css.splitlines() if line.startswith(".btn, :where(button[type=submit]")
    )
    assert "button.secondary" in rule and "button.danger" in rule
