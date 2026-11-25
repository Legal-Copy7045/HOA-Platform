from django.core import mail
from django.core.mail import EmailMessage

from core.email import RedirectingBackend


def test_redirect_sends_everything_to_one_inbox(settings):
    settings.EMAIL_TRANSPORT_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.EMAIL_REDIRECT_ALL_TO = "catchall@example.org"
    mail.outbox = []
    message = EmailMessage(
        "Hello", "Body", "from@example.com", ["owner7@example.com"], cc=["co@example.com"]
    )
    RedirectingBackend().send_messages([message])
    sent = mail.outbox[0]
    assert sent.to == ["catchall@example.org"]
    assert sent.cc == []
    assert sent.subject == "[to: owner7@example.com, co@example.com] Hello"


def test_no_redirect_when_unset(settings):
    settings.EMAIL_TRANSPORT_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.EMAIL_REDIRECT_ALL_TO = ""
    mail.outbox = []
    RedirectingBackend().send_messages(
        [EmailMessage("Hi", "B", "from@example.com", ["owner7@example.com"])]
    )
    assert mail.outbox[0].to == ["owner7@example.com"]
