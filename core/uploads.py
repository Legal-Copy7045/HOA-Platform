"""Stop oversized uploads before they are buffered (security review 2026-11).

Django's multipart parser normally streams the whole request to a temporary
file before any view can check its size. ``RequestSizeLimitMiddleware``
refuses a request whose declared length is too big, and
``LimitedUploadHandler`` stops a file part (for example a chunked request
with no length) as soon as it passes the limit.
"""

from __future__ import annotations

from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler, StopUpload
from django.http import HttpResponse

# Room for form fields and multipart framing around the largest allowed file.
_OVERHEAD = 1024 * 1024


def max_request_bytes() -> int:
    return settings.DOCUMENT_MAX_BYTES + _OVERHEAD


class RequestSizeLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            return HttpResponse("Bad request.", status=400, content_type="text/plain")
        if length > max_request_bytes():
            return HttpResponse("The upload is too large.", status=413, content_type="text/plain")
        return self.get_response(request)


class LimitedUploadHandler(FileUploadHandler):
    """Listed first in FILE_UPLOAD_HANDLERS; passes data on unchanged."""

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self._received = 0

    def receive_data_chunk(self, raw_data, start):
        self._received += len(raw_data)
        if self._received > settings.DOCUMENT_MAX_BYTES:
            raise StopUpload(connection_reset=True)
        return raw_data

    def file_complete(self, file_size):
        return None
