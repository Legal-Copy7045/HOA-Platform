import pytest


@pytest.mark.django_db
def test_every_response_has_a_server_generated_request_id(client):
    response = client.get("/healthz", HTTP_X_REQUEST_ID="client-chosen")
    request_id = response["X-Request-ID"]
    assert request_id != "client-chosen"
    assert len(request_id) == 32
