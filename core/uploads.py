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
            # A page people can act on, not a bare error (review R2-004).
            # Rendered without the site's database-backed context: this check
            # runs before anything else and must stay cheap.
            from django.template.loader import render_to_string

            body = render_to_string(
                "upload_too_large.html",
                {"limit_mb": settings.DOCUMENT_MAX_BYTES // (1024 * 1024)},
            )
            return HttpResponse(body, status=413)
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


class SmallUploadHandler(FileUploadHandler):
    """For a view that takes only small files (the register import, D54):
    stops the upload once it passes ``limit`` bytes, before Django buffers
    it, and marks the request so the view can say why. Installed by the view
    in place of the default handlers, with only the in-memory handler after
    it, so the file never touches local disk."""

    def __init__(self, request=None, limit: int = 0):
        super().__init__(request)
        self.limit = limit

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self._received = 0

    def receive_data_chunk(self, raw_data, start):
        self._received += len(raw_data)
        if self._received > self.limit:
            self.request.upload_too_large = True
            raise StopUpload(connection_reset=False)
        return raw_data

    def file_complete(self, file_size):
        return None


class _MemoryOnly(FileUploadHandler):
    """Keeps a small file in memory whatever its size (the limit before it
    has already stopped anything large)."""

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        import io

        self._buffer = io.BytesIO()

    def receive_data_chunk(self, raw_data, start):
        self._buffer.write(raw_data)
        return None

    def file_complete(self, file_size):
        from django.core.files.uploadedfile import InMemoryUploadedFile

        self._buffer.seek(0)
        return InMemoryUploadedFile(
            file=self._buffer,
            field_name=self.field_name,
            name=self.file_name,
            content_type=self.content_type,
            size=file_size,
            charset=self.charset,
            content_type_extra=self.content_type_extra,
        )


def limit_uploads(request, limit: int) -> bool:
    """Call before anything reads ``request.POST`` or ``request.FILES`` (the
    view must be ``csrf_exempt`` with ``csrf_protect`` inside). Returns False
    when the declared request size is already over the limit."""
    request.upload_too_large = False
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length > limit + _OVERHEAD:
        request.upload_too_large = True
        return False
    request.upload_handlers = [SmallUploadHandler(request, limit), _MemoryOnly(request)]
    return True
