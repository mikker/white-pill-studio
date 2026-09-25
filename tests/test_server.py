import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from studio import StudioHandler


FIXTURE = """\
[palette.light]
accent = "#1853C7"

[color.light]
accent = "@palette.light.accent"
surface-raised-alpha = 0.78

[color.dark]
accent = "#1853C7"

[spacing]
control-height = 28

[recipe.window]
active-border = "@color.{mode}.accent"
"""

GENERATOR = """\
#!/bin/sh
echo "generated $1 from $(pwd)"
printf 'stamp' > generated.txt
"""


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls.directory.name)
        cls.root = root
        cls.source = root / "design-system" / "tokens.toml"
        cls.source.parent.mkdir()
        cls.source.write_text(FIXTURE)
        generator = root / "script" / "design-system"
        generator.parent.mkdir()
        generator.write_text(GENERATOR)
        generator.chmod(generator.stat().st_mode | stat.S_IXUSR)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StudioHandler)
        cls.server.source = cls.source
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.directory.cleanup()

    def setUp(self):
        self.source.write_text(FIXTURE)
        (self.root / "generated.txt").unlink(missing_ok=True)

    def call(self, path, body=None, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                payload = error.read()
            try:
                return error.code, json.loads(payload)
            except json.JSONDecodeError:
                return error.code, payload

    def test_serves_the_studio_and_static_assets(self):
        for path, expected_type in (("/", "text/html"), ("/studio.js", "text/javascript"), ("/studio.css", "text/css")):
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(expected_type, response.headers["Content-Type"])

    def test_serves_the_window_fixtures(self):
        for path in ("/fixtures/browser-page.html", "/fixtures/browser-page.html?scheme=dark"):
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("text/html", response.headers["Content-Type"])
                page = response.read().decode()
            self.assertIn("<h1>Mekanikos</h1>", page)
            self.assertIn('data-theme="dark"', page)  # ?scheme=dark support
            self.assertNotRegex(page, r"(src|href)=\"https?://")  # self-contained
        with urllib.request.urlopen(self.base + "/fixtures/terminal-session.ans", timeout=5) as response:
            self.assertEqual(response.status, 200)
            session = response.read().decode()
        lines = re.sub(r"\x1b\[[0-9;]*m", "", session).split("\n")
        self.assertEqual(lines, [
            "~/dev/white-pill-studio  main*",
            "$ ls",
            "bin  captures  design-system  references  script  studio.py  tests  web",
            "$ git status --short",
            " M design-system/tokens.toml",
            "?? captures/window-light.png",
            "$ script/design-system check",
            "studio    current (1 file)",
            "dotfiles  current (27 files)",
            "Design-system outputs are current",
            "$ ",
            "■■■■■■■■  ■■■■■■■■",
            "",
        ])
        self.assertTrue(all(len(line) <= 100 for line in lines) and len(lines) <= 28)
        self.assertIn("\x1b[34m~/dev/white-pill-studio\x1b[0m  \x1b[35mmain*\x1b[0m", session)
        self.assertIn("\x1b[1;34mbin\x1b[0m", session)
        self.assertIn("\x1b[97m■\x1b[0m", session)

    def test_refuses_paths_outside_the_web_root(self):
        for path in ("/../studio.py", "/%2e%2e/studio.py", "/missing.js"):
            status, _ = self.call(path)
            self.assertEqual(status, 404, path)

    def test_state_reports_tokens_and_source(self):
        status, payload = self.call("/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source"], str(self.source))
        self.assertEqual(payload["tokens"]["spacing"]["control-height"], 28)

    def test_state_reports_references_provenance_manifest_and_issues(self):
        status, payload = self.call("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(payload["tokens"]["color"]["light"]["accent"], "#1853C7")
        self.assertEqual(payload["raw"]["color"]["light"]["accent"], "@palette.light.accent")
        self.assertEqual(payload["tokens"]["recipe"]["window"]["active-border"], "@color.{mode}.accent")
        self.assertEqual(payload["readOnlySections"], ["recipe", "meta"])
        accent = payload["provenance"]["color.light.accent"]
        self.assertEqual(accent["resolvesFrom"], "palette.light.accent")
        self.assertEqual(accent["chain"], ["palette.light.accent"])
        self.assertEqual(accent["referencedBy"], ["recipe.window.active-border"])
        self.assertEqual(accent["usedBy"][0]["recipe"], "window")
        self.assertEqual(accent["usedBy"][0]["consumers"][0]["key"], "active-border")
        self.assertIn("palette.light.accent", payload["provenance"])
        self.assertTrue(payload["manifest"]["targets"])
        self.assertEqual(payload["issues"], [])

    def test_validate_reports_issues_without_writing(self):
        changes = {"color.light.surface-raised-alpha": 1.5, "color.light.accent": "@palette.light.nope"}
        status, payload = self.call("/api/validate", {"changes": changes})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        found = {(item["code"], item["path"], item["level"]) for item in payload["issues"]}
        self.assertIn(("opacity", "color.light.surface-raised-alpha", "error"), found)
        self.assertIn(("reference", "color.light.accent", "error"), found)
        self.assertEqual(self.source.read_text(), FIXTURE)

        status, payload = self.call("/api/validate", {"changes": {}})
        self.assertEqual((status, payload["issues"]), (200, []))

    def test_validate_rejects_malformed_changes(self):
        status, payload = self.call("/api/validate", {"changes": {"spacing.nope": 1}})
        self.assertEqual(status, 400)
        self.assertIn("Unknown token", payload["error"])

    def test_save_refuses_validation_errors(self):
        for changes in (
            {"color.light.surface-raised-alpha": 1.5},
            {"color.light.accent": "@color.light.accent"},
        ):
            status, payload = self.call("/api/save", {"changes": changes})
            self.assertEqual(status, 400, changes)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"], "Validation failed")
            self.assertTrue(any(item["level"] == "error" for item in payload["issues"]))
        self.assertEqual(self.source.read_text(), FIXTURE)
        self.assertFalse((self.root / "generated.txt").exists())

    def test_save_allows_warnings_and_relinking(self):
        status, payload = self.call("/api/save", {"changes": {"color.light.accent": "#FF0000"}})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["raw"]["color"]["light"]["accent"], "#FF0000")
        self.assertIsNone(payload["provenance"]["color.light.accent"]["resolvesFrom"])
        status, payload = self.call("/api/save", {"changes": {"color.light.accent": "@palette.light.accent"}})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.source.read_text(), FIXTURE)
        self.assertIn("manifest", payload)
        self.assertIn("result", payload)

    def test_save_updates_source_and_runs_the_discovered_generator(self):
        status, payload = self.call("/api/save", {"changes": {"spacing.control-height": 30}})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tokens"]["spacing"]["control-height"], 30)
        self.assertIn("control-height = 30", self.source.read_text())
        self.assertIn(f"generated generate from {self.root}", payload["result"]["output"])
        self.assertEqual((self.root / "generated.txt").read_text(), "stamp")

    def test_generate_runs_without_changes(self):
        status, payload = self.call("/api/generate", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["command"], f"{self.root / 'script' / 'design-system'} generate")

    def test_save_rejects_bad_changes_without_touching_the_source(self):
        cases = (
            ({"changes": {"spacing.nope": 1}}, "Unknown token"),
            ({"changes": {"spacing.control-height": "tall"}}, "Wrong value type"),
            ({"changes": {}}, "No token changes"),
            ({"changes": [1]}, "No token changes"),
        )
        for body, message in cases:
            status, payload = self.call("/api/save", body)
            self.assertEqual(status, 400, body)
            self.assertIn(message, payload["error"])
        self.assertEqual(self.source.read_text(), FIXTURE)

    def test_rejects_malformed_json(self):
        status, payload = self.call("/api/save", raw=b"not json")
        self.assertEqual(status, 400)
        self.assertIn("Invalid JSON", payload["error"])

    def test_unknown_api_route(self):
        status, _ = self.call("/api/nothing", {})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
