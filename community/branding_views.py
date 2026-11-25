from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET

from community import branding
from community.models import LoginBackground
from core import policy


@login_required
def branding_settings(request):
    policy.require(request.user, "branding.manage")
    if request.method == "POST":
        variant = request.POST.get("variant") or "desktop"
        try:
            if request.POST.get("action") == "remove":
                branding.clear_login_background(
                    variant=variant, actor=request.user, request=request
                )
                messages.success(request, "Picture removed from the sign-in page.")
            elif "image" in request.FILES:
                branding.set_login_background(
                    request.FILES["image"], variant=variant, actor=request.user, request=request
                )
                messages.success(request, "New sign-in background saved.")
            else:
                messages.error(request, "Choose a picture to upload.")
        except branding.BrandingError as exc:
            messages.error(request, str(exc))
        return redirect("branding_settings")
    current = branding.current_backgrounds()
    slots = [
        {
            "variant": "desktop",
            "title": "Desktop and tablet",
            "image": current["desktop"],
            "hint": "Landscape, at least 1920 by 1080.",
        },
        {
            "variant": "phone",
            "title": "Phone",
            "image": current["phone"],
            "hint": "Portrait, at least 1080 by 1920. Without one, phones use the desktop picture.",
        },
    ]
    return render(
        request,
        "community/branding.html",
        {
            "slots": slots,
            "history": LoginBackground.objects.select_related("uploaded_by")[:12],
            "max_mb": branding.MAX_UPLOAD_BYTES // (1024 * 1024),
        },
    )


@require_GET
def login_background_image(request, pk):
    """Public on purpose: the sign-in page is seen before anyone signs in.
    Only the current picture is served; it holds no personal data."""
    current = next(
        (img for img in branding.current_backgrounds().values() if img and img.pk == pk), None
    )
    if current is None:
        raise Http404
    response = FileResponse(current.file.open("rb"), content_type=current.content_type)
    response["Cache-Control"] = "public, max-age=86400"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response
