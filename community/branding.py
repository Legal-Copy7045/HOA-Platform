"""Login page background: upload, malware scan, re-encode, serve (no personal data)."""

from __future__ import annotations

import io

from django.core.files.base import ContentFile

from audit.services import record_event
from community.models import LoginBackground
from core.policy import require
from documents.validation import UploadRejected, check_upload

MAX_SIDE = 2560
VARIANTS = (LoginBackground.Variant.DESKTOP, LoginBackground.Variant.PHONE)
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 60_000_000  # decompression-bomb guard (about 8000 x 7500)


class BrandingError(ValueError):
    pass


def _reencode(uploaded) -> tuple[bytes, str, int, int]:
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        uploaded.seek(0)
        with Image.open(uploaded) as img:
            if img.format not in ("JPEG", "PNG"):
                raise BrandingError("Use a JPEG or PNG picture.")
            img.load()
            img = ImageOps.exif_transpose(img)  # keep the intended orientation
            img.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            # A fresh JPEG with no metadata: no EXIF/GPS, no comments, no
            # trailing data from the original file.
            img.convert("RGB").save(out, "JPEG", quality=85, optimize=True, progressive=True)
            return out.getvalue(), "image/jpeg", img.width, img.height
    except BrandingError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise BrandingError("That picture could not be read. Try a JPEG or PNG.") from exc


def scan(fileobj):  # a seam for tests, like documents.services.scan
    from documents.scanning import scan as run

    return run(fileobj)


def _scan_gate(uploaded) -> None:
    """The original upload passes the malware scan before Pillow decodes it
    (D87): the same rule as every stored file (``scan_allows_download``), and
    since the picture is shown without sign-in, nothing unscanned is kept
    where scanning is required."""
    from documents.models import ScanResult
    from documents.services import scan_allows_download

    uploaded.seek(0)
    result, _scanner = scan(uploaded)
    uploaded.seek(0)
    if result == ScanResult.INFECTED:
        raise BrandingError("The picture was flagged by the malware scanner and was not used.")
    if not scan_allows_download(result):
        raise BrandingError(
            "The picture could not be checked for malware just now. Try again in a few minutes."
        )


def _variant(value: str) -> str:
    if value not in VARIANTS:
        raise BrandingError("Choose desktop or phone.")
    return value


def set_login_background(
    uploaded, *, variant: str = LoginBackground.Variant.DESKTOP, actor, request=None
) -> LoginBackground:
    require(actor, "branding.manage")
    variant = _variant(variant)
    if uploaded.size > MAX_UPLOAD_BYTES:
        raise BrandingError(f"Pictures must be {MAX_UPLOAD_BYTES // (1024 * 1024)} MB or smaller.")
    try:
        check_upload(uploaded, allowed={"jpg", "jpeg", "png"})
    except UploadRejected as exc:
        raise BrandingError(str(exc)) from exc
    _scan_gate(uploaded)
    data, content_type, width, height = _reencode(uploaded)
    image = LoginBackground(
        variant=variant,
        content_type=content_type,
        width=width,
        height=height,
        uploaded_by=actor,
    )
    image.file.save("background.jpg", ContentFile(data), save=False)
    image.save()
    record_event(
        "branding.login_background_set",
        actor=actor,
        request=request,
        target=image,
        metadata={"variant": variant, "width": width, "height": height},
    )
    return image


def clear_login_background(
    *, variant: str = LoginBackground.Variant.DESKTOP, actor, request=None
) -> None:
    require(actor, "branding.manage")
    variant = _variant(variant)
    image = LoginBackground.objects.create(variant=variant, uploaded_by=actor)
    record_event(
        "branding.login_background_removed",
        actor=actor,
        request=request,
        target=image,
        metadata={"variant": variant},
    )


def current_login_background(
    variant: str = LoginBackground.Variant.DESKTOP,
) -> LoginBackground | None:
    latest = LoginBackground.objects.filter(variant=variant).first()
    return latest if latest and latest.file else None


def current_backgrounds() -> dict[str, LoginBackground | None]:
    """{"desktop": ..., "phone": ...}; either may be None."""
    return {v: current_login_background(v) for v in VARIANTS}
