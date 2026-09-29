"""Build browser-side downloads that do not depend on Streamlit media storage."""

import base64
from html import escape


def build_download_link(data: bytes | str, filename: str, mime: str, label: str) -> str:
    """Embed a small generated file in a link sent with the Streamlit page."""
    content = data.encode("utf-8") if isinstance(data, str) else data
    encoded = base64.b64encode(content).decode("ascii")
    return (
        f'<a href="data:{escape(mime, quote=True)};base64,{encoded}" '
        f'download="{escape(filename, quote=True)}" '
        'style="display:inline-block;padding:0.5rem 0.75rem;'
        'border:1px solid currentColor;border-radius:0.5rem;text-decoration:none">'
        f'{escape(label)}</a>'
    )
