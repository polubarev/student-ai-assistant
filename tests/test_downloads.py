"""Regression checks for downloads from a replicated Streamlit deployment."""

import ast
import base64
from pathlib import Path
import re
import unittest

from utils.downloads import build_download_link


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


class _Container:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _StreamlitStub:
    def __init__(self):
        self.session_state = {
            "summary": "Тестовое саммари",
            "transcript": "Тестовая транскрипция",
        }
        self.markdown_calls = []
        self.download_calls = []

    def tabs(self, labels, **_):
        return [_Container() for _ in labels]

    def columns(self, widths):
        return [_Container() for _ in range(widths if isinstance(widths, int) else len(widths))]

    def markdown(self, body, **_):
        self.markdown_calls.append(body)

    def download_button(self, **kwargs):
        self.download_calls.append(kwargs)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _render_results():
    # Execute the real call site without importing Streamlit or external APIs.
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "section_results"
    )
    st = _StreamlitStub()
    namespace = {
        "st": st,
        "build_summary_pdf_bytes": lambda _: b"%PDF-1.4\nfixture",
        "logger": type("Logger", (), {"exception": lambda *_: None})(),
    }
    namespace["build_download_link"] = build_download_link
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(APP_PATH), "exec"), namespace)
    namespace["section_results"]()
    return st


def _download_links(markdown_calls):
    return {
        filename: base64.b64decode(payload)
        for body in markdown_calls
        for payload, filename in re.findall(
            r'<a\b[^>]*href="data:[^;]+;base64,([A-Za-z0-9+/=]+)"[^>]*download="([^"]+)"',
            body,
        )
    }


class DownloadTests(unittest.TestCase):
    def test_results_downloads_do_not_need_a_second_server_request(self):
        st = _render_results()
        links = _download_links(st.markdown_calls)
        self.assertEqual(links["summary.pdf"], b"%PDF-1.4\nfixture")
        self.assertEqual(links["summary.txt"], "Тестовое саммари".encode("utf-8"))
        self.assertEqual(links["transcript.txt"], "Тестовая транскрипция".encode("utf-8"))
        self.assertEqual(st.download_calls, [])


if __name__ == "__main__":
    unittest.main()
