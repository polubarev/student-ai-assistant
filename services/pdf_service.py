"""Static HTML-to-PDF export with no external resources and bounded worker lifetime."""

import os
import subprocess
import sys
import tempfile


MAX_HTML_BYTES = 1024 * 1024
MAX_PDF_BYTES = 16 * 1024 * 1024


def deny_external_resource(url, *args, **kwargs):
    raise ValueError("External PDF resources are disabled")


def render_pdf(document_html):
    encoded = document_html.encode("utf-8")
    if len(encoded) > MAX_HTML_BYTES:
        raise ValueError("Document exceeds the PDF export limit")
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "FONTCONFIG_PATH", "FONTCONFIG_FILE"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    with tempfile.TemporaryDirectory(prefix="pdf_export_") as scratch:
        environment.update(HOME=scratch, TMPDIR=scratch, TEMP=scratch, TMP=scratch, XDG_CACHE_HOME=scratch)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "services.pdf_service"], input=encoded,
                capture_output=True, check=True, timeout=30, env=environment,
            )
        except (subprocess.SubprocessError, OSError):
            raise RuntimeError("PDF export could not be completed") from None
    if not result.stdout.startswith(b"%PDF") or len(result.stdout) > MAX_PDF_BYTES:
        raise RuntimeError("Invalid PDF export")
    return result.stdout


def worker():
    if sys.platform.startswith("linux"):
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024, 384 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    document = sys.stdin.buffer.read(MAX_HTML_BYTES + 1)
    if len(document) > MAX_HTML_BYTES:
        raise ValueError("Document exceeds the PDF export limit")
    from weasyprint import HTML
    from weasyprint.urls import URLFetcher

    class RestrictedFetcher(URLFetcher):
        def fetch(self, url, headers=None):
            return deny_external_resource(url)

    result = HTML(string=document.decode("utf-8"), url_fetcher=RestrictedFetcher()).write_pdf()
    if len(result) > MAX_PDF_BYTES:
        raise ValueError("PDF exceeds the export limit")
    sys.stdout.buffer.write(result)


if __name__ == "__main__":
    worker()
