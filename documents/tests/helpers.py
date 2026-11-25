import io
import zipfile

from django.core.files.uploadedfile import SimpleUploadedFile


def pdf(name="minutes.pdf", body=b"%PDF-1.7\n1 0 obj\n%%EOF\n"):
    return SimpleUploadedFile(name, body, content_type="application/pdf")


def docx(name="form.docx", macros=False):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("word/document.xml", "<w:document/>")
        if macros:
            z.writestr("word/vbaProject.bin", b"\x00")
    return SimpleUploadedFile(name, buffer.getvalue())
