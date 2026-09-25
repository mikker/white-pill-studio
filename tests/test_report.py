import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import adapters  # noqa: E402
import report  # noqa: E402
import studio  # noqa: E402
import tokens_model  # noqa: E402

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "tokens.toml"
SCRIPT = ROOT / "script" / "design-system"


def consumers(root="/studio", dotfiles="/dotfiles"):
    return [
        adapters.Consumer("studio", pathlib.Path(root), ("studio-css",), "studio"),
        adapters.Consumer("dotfiles", pathlib.Path(dotfiles), adapters.FAMILIES, "config"),
    ]


def draft_of(raw, changes):
    _, changed = studio.prepare_changes(raw, changes)
    return changed


class ChangeReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = tokens_model.load(FIXTURE)

    def build(self, changes):
        return report.change_report(self.raw, draft_of(self.raw, changes), consumers())

    def test_token_diff_carries_raw_and_resolved_values(self):
        result = self.build({"color.light.accent": "#286486"})
        self.assertEqual(result["tokens"], [{
            "path": "color.light.accent", "oldRaw": "@palette.light.accent", "newRaw": "#286486",
            "oldValue": "#1853C7", "newValue": "#286486",
        }])

    def test_field_diffs_are_grouped_by_consumer_and_file(self):
        result = self.build({"color.light.accent": "#286486"})
        displays = [target["display"] for target in result["targets"]]
        self.assertEqual(displays[0], "studio:design-system/tokens.css")
        self.assertIn("dotfiles:omarchy-themes/mekanikos-light/shell.hyprland.toml", displays)
        self.assertFalse(any("mekanikos/" in display for display in displays), "dark files must not change")
        hyprland = next(t for t in result["targets"] if t["display"].endswith("shell.hyprland.toml"))
        self.assertEqual(hyprland["consumer"], "dotfiles")
        self.assertEqual(hyprland["path"], "/dotfiles/omarchy-themes/mekanikos-light/shell.hyprland.toml")
        self.assertEqual(hyprland["mode"], "light")
        self.assertEqual(
            [{key: field[key] for key in ("section", "key", "old", "new")} for field in hyprland["fields"]],
            [{"section": "hyprland", "key": "active-border", "old": "#1853C7", "new": "#286486"}],
        )
        css = result["targets"][0]
        accent = next(field for field in css["fields"] if field["key"] == "--color-accent")
        self.assertEqual((accent["section"], accent["old"], accent["new"]), ("light", "#1853C7", "#286486"))
        # consumer summaries and totals agree with the targets
        self.assertEqual([item["name"] for item in result["consumers"]], ["studio", "dotfiles"])
        self.assertEqual(sum(item["fields"] for item in result["consumers"]), result["totals"]["fields"])
        self.assertEqual(result["totals"], {
            "tokens": 1, "targets": len(result["targets"]),
            "fields": sum(len(target["fields"]) for target in result["targets"]),
        })

    def test_recipe_changes_resolve_per_mode(self):
        result = self.build({"recipe.window.active-border": "@color.{mode}.danger"})
        token = result["tokens"][0]
        self.assertEqual(token["oldValue"], "#1853C7")  # equal in both modes, so collapsed
        self.assertIsInstance(token["newValue"], dict)
        self.assertEqual(set(token["newValue"]), {"light", "dark"})
        files = {target["display"] for target in result["targets"]}
        self.assertEqual(files, {
            "dotfiles:omarchy-themes/mekanikos/shell.hyprland.toml",
            "dotfiles:omarchy-themes/mekanikos-light/shell.hyprland.toml",
            "dotfiles:omarchy-themes/mekanikos/hyprland_window.lua",
            "dotfiles:omarchy-themes/mekanikos-light/hyprland_window.lua",
        })

    def test_no_changes_is_an_empty_report(self):
        result = report.change_report(self.raw, self.raw, consumers())
        self.assertEqual(result["totals"], {"tokens": 0, "targets": 0, "fields": 0})
        self.assertEqual((result["tokens"], result["targets"], result["issues"]), ([], [], []))

    def test_issues_describe_the_draft(self):
        result = self.build({"color.light.text-primary": "#C8C8C8"})
        self.assertTrue(any(item["code"] == "contrast" and item["level"] == "error" for item in result["issues"]))

    def test_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            before = sorted(pathlib.Path(directory).rglob("*"))
            report.change_report(self.raw, draft_of(self.raw, {"spacing.control-height": 30}),
                                 consumers(directory, pathlib.Path(directory) / "dotfiles"))
            self.assertEqual(sorted(pathlib.Path(directory).rglob("*")), before)

    def test_text_form(self):
        text = report.format_report(self.build({"color.light.accent": "#286486", "spacing.control-height": 30}))
        self.assertTrue(text.startswith("Change report: 2 tokens → "))
        self.assertRegex(text, r"color\.light\.accent +@palette\.light\.accent → #286486 +\(resolved #1853C7 → #286486\)")
        self.assertIn("  dotfiles:omarchy-themes/mekanikos-light/shell.hyprland.toml\n    hyprland.active-border: #1853C7 → #286486", text)
        self.assertIn("spacing.control-height: 28 → 30", text)

    def test_parse_assignment(self):
        self.assertEqual(report.parse_assignment("color.light.accent=#286486"), ("color.light.accent", "#286486"))
        self.assertEqual(report.parse_assignment("spacing.control-height=30"), ("spacing.control-height", 30))
        self.assertEqual(report.parse_assignment("chrome.light.scrim-alpha=0.5"), ("chrome.light.scrim-alpha", 0.5))
        self.assertEqual(report.parse_assignment("spacing.scale-with-font=true"), ("spacing.scale-with-font", True))
        self.assertEqual(report.parse_assignment('type.ui="Inter"'), ("type.ui", "Inter"))
        self.assertEqual(report.parse_assignment("color.light.accent=@palette.light.blue"),
                         ("color.light.accent", "@palette.light.blue"))
        with self.assertRaises(ValueError):
            report.parse_assignment("no-equals")


class ReportCommandTest(unittest.TestCase):
    """script/design-system report against a temporary studio root."""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls.directory.name)
        cls.source = root / "design-system" / "tokens.toml"
        cls.source.parent.mkdir()
        cls.source.write_text(FIXTURE.read_text())
        (root / "design-system" / "consumers.toml").write_text(
            f'[[consumer]]\nname = "dotfiles"\nroot = "{root / "dotfiles"}"\nadapters = ["omarchy-theme", "hunk"]\n')
        cls.env = {key: value for key, value in os.environ.items()
                   if key not in ("WHITE_PILL_DOTFILES", "WHITE_PILL_CONSUMERS")}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_report(self, *args, stdin=None):
        return subprocess.run([sys.executable, str(SCRIPT), "--source", str(self.source), "report", *args],
                              capture_output=True, text=True, env=self.env, input=stdin)

    def test_set_prints_a_readable_report(self):
        result = self.run_report("--set", "color.light.accent=#286486")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("color.light.accent  @palette.light.accent → #286486", result.stdout)
        self.assertIn("dotfiles:omarchy-themes/mekanikos-light/shell.hyprland.toml", result.stdout)
        self.assertEqual(self.source.read_text(), FIXTURE.read_text())

    def test_changes_file_and_json_output(self):
        changes = pathlib.Path(self.directory.name) / "changes.json"
        changes.write_text(json.dumps({"changes": {"spacing.control-height": 30}}))
        result = self.run_report(str(changes), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["tokens"][0]["path"], "spacing.control-height")
        self.assertEqual({item["consumer"] for item in payload["targets"]}, {"studio", "dotfiles"})
        result = self.run_report("-", "--json", stdin=json.dumps({"type.ui": "Inter Display"}))
        self.assertEqual(json.loads(result.stdout)["tokens"][0]["newRaw"], "Inter Display")

    def test_validation_errors_exit_1(self):
        result = self.run_report("--set", "color.light.surface-raised-alpha=1.5")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Validation: 1 error", result.stdout)

    def test_bad_changes_exit_1(self):
        result = self.run_report("--set", "spacing.nope=1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Unknown token", result.stderr)


REPORT_FIXTURE = """\
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


class ReportEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls.directory.name)
        cls.source = root / "design-system" / "tokens.toml"
        cls.source.parent.mkdir()
        cls.source.write_text(REPORT_FIXTURE)
        (root / "design-system" / "consumers.toml").write_text(
            '[[consumer]]\nname = "dots"\nroot = "dots"\nadapters = ["omarchy-theme"]\n')
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), studio.StudioHandler)
        cls.server.source = cls.source
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.directory.cleanup()

    def post(self, body):
        request = urllib.request.Request(self.base + "/api/report", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_reports_tokens_targets_totals_and_issues(self):
        status, payload = self.post({"changes": {"color.light.accent": "#286486"}})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        result = payload["report"]
        self.assertEqual(set(result), {"tokens", "targets", "consumers", "totals", "issues"})
        self.assertEqual(result["tokens"], [{
            "path": "color.light.accent", "oldRaw": "@palette.light.accent", "newRaw": "#286486",
            "oldValue": "#1853C7", "newValue": "#286486",
        }])
        hyprland = next(t for t in result["targets"] if t["display"] == "dots:omarchy-themes/mekanikos-light/shell.hyprland.toml")
        self.assertEqual(hyprland["consumer"], "dots")
        self.assertEqual(hyprland["path"], str(pathlib.Path(self.directory.name).resolve() / "dots/omarchy-themes/mekanikos-light/shell.hyprland.toml"))
        field = next(f for f in hyprland["fields"] if f["key"] == "active-border")
        self.assertEqual((field["section"], field["old"], field["new"]), ("hyprland", "#1853C7", "#286486"))
        self.assertEqual(result["totals"]["tokens"], 1)
        self.assertEqual(result["totals"]["targets"], len(result["targets"]))
        self.assertIsInstance(result["issues"], list)
        self.assertEqual(self.source.read_text(), REPORT_FIXTURE)

    def test_empty_changes(self):
        status, payload = self.post({"changes": {}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["report"]["totals"], {"tokens": 0, "targets": 0, "fields": 0})

    def test_validation_issues_are_reported_not_refused(self):
        status, payload = self.post({"changes": {"color.light.surface-raised-alpha": 1.5}})
        self.assertEqual(status, 200)
        self.assertIn(("opacity", "error"), {(item["code"], item["level"]) for item in payload["report"]["issues"]})

    def test_bad_changes_are_refused(self):
        for body, message in (({"changes": {"spacing.nope": 1}}, "Unknown token"),
                              ({"changes": [1]}, "must be an object"),
                              ({"changes": {"spacing.control-height": "tall"}}, "Wrong value type")):
            status, payload = self.post(body)
            self.assertEqual(status, 400, body)
            self.assertIn(message, payload["error"])


if __name__ == "__main__":
    unittest.main()
