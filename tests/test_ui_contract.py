"""Verify the packaged UI can be served under the real local security contract.

These tests use no browser or synthetic DOM. The separate browser_smoke gate
exercises real user actions, media playback, jobs and downloads in CI.
"""

from html.parser import HTMLParser
from importlib.resources import files
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit

from frameflow.api import create_app


class ShellParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.labels = []
        self.resources = []
        self.inline_handlers = []
        self.inline_executable = []
        self._script_src = None
        self._in_script = False

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if "id" in attributes:
            self.ids.append(attributes["id"])
        if tag == "label" and "for" in attributes:
            self.labels.append(attributes["for"])
        self.inline_handlers.extend(name for name in attributes if name.lower().startswith("on"))
        if tag == "script":
            self._in_script = True
            self._script_src = attributes.get("src")
            if self._script_src:
                self.resources.append(self._script_src)
        if tag == "link" and attributes.get("rel") == "stylesheet":
            self.resources.append(attributes.get("href", ""))
        if tag == "style":
            self.inline_executable.append("style")

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False
            self._script_src = None

    def handle_data(self, data):
        if self._in_script and not self._script_src and data.strip():
            self.inline_executable.append("script")


class UIContractTests(unittest.TestCase):
    def setUp(self):
        self.parser = ShellParser()
        self.parser.feed(files("frameflow").joinpath("static", "index.html").read_text(encoding="utf-8"))

    def test_all_declared_resources_are_packaged_and_have_real_get_routes(self):
        """Catch broken /static aliases and missing wheel package-data assets."""
        self.assertTrue(self.parser.resources)
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory), token="contract-only")
            routes = {route.path: route for route in app.routes if "GET" in getattr(route, "methods", set())}
            for resource in self.parser.resources:
                with self.subTest(resource=resource):
                    parsed = urlsplit(resource)
                    self.assertFalse(parsed.scheme or parsed.netloc, "The local UI must not require a CDN.")
                    self.assertTrue(parsed.path.startswith("/"))
                    self.assertIn(parsed.path, routes)
                    response = routes[parsed.path].endpoint()
                    self.assertTrue(Path(response.path).is_file())
                    self.assertTrue(Path(response.path).read_bytes())

    def test_shell_is_compatible_with_the_same_origin_csp(self):
        """Inline execution would silently fail under api.py's CSP."""
        self.assertEqual(self.parser.inline_handlers, [])
        self.assertEqual(self.parser.inline_executable, [])
        self.assertEqual(len(self.parser.ids), len(set(self.parser.ids)), "Duplicate IDs can target the wrong project control.")
        for target in self.parser.labels:
            with self.subTest(label=target):
                self.assertIn(target, self.parser.ids, "A visible field label has no input target.")

    def test_browser_failure_diagnostic_keeps_location_without_raw_exception_secrets(self):
        from tools.browser_smoke import failure_diagnostic
        import json

        try:
            raise TimeoutError("Navigation to https://local.invalid/#token=private-token at /private/media/source.mov timed out")
        except TimeoutError as error:
            diagnostic = failure_diagnostic(error, "play-common-timeline")
        self.assertEqual(diagnostic["stage"], "play-common-timeline")
        self.assertEqual(diagnostic["exception"], "TimeoutError")
        self.assertTrue(diagnostic["locations"])
        self.assertEqual(diagnostic["locations"][-1]["file"], "tests/test_ui_contract.py")
        serialized = json.dumps(diagnostic)
        for secret in ("private-token", "https://", "/private/media", str(Path.cwd())):
            self.assertNotIn(secret, serialized)

    def test_engine_work_is_submitted_by_post_and_artifact_download_is_read_only(self):
        """Protect the HTTP boundary used by the browser workflow gate."""
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Path(directory), token="contract-only")
            methods = {}
            for route in app.routes:
                methods.setdefault(route.path, set()).update(getattr(route, "methods", set()))
            for path in ("/api/projects/{project_id}/jobs", "/api/projects/{project_id}/exports"):
                with self.subTest(path=path):
                    self.assertIn("POST", methods[path])
            self.assertNotIn("GET", methods["/api/projects/{project_id}/exports"])
            self.assertEqual(methods["/api/jobs/{job_id}/download"], {"GET"})
            self.assertEqual(methods["/api/projects/{project_id}/review"], {"POST"})


if __name__ == "__main__":
    unittest.main()
