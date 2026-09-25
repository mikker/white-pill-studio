import json
import os
import pathlib
import subprocess
import tempfile
import unittest

import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from studio import StudioError, read_tokens, update_source, value_at


FIXTURE = """\
[color.light]
accent = "#1853C7" # keep this note
surface-raised-alpha = 0.78

[spacing]
control-height = 28
scale-with-font = false

[type]
ui = "Inter"
"""


class TokenSourceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.source = pathlib.Path(self.directory.name) / "tokens.toml"
        self.source.write_text(FIXTURE)

    def tearDown(self):
        self.directory.cleanup()

    def test_updates_scalars_without_reformatting_the_document(self):
        update_source(
            self.source,
            {
                "color.light.accent": "#286486",
                "color.light.surface-raised-alpha": 0.8,
                "spacing.control-height": 30,
                "spacing.scale-with-font": True,
            },
        )

        text = self.source.read_text()
        self.assertIn('accent = "#286486" # keep this note', text)
        self.assertIn("surface-raised-alpha = 0.8", text)
        self.assertIn("control-height = 30", text)
        self.assertIn("scale-with-font = true", text)
        self.assertEqual(value_at(read_tokens(self.source), "spacing.control-height"), 30)

    def test_rejects_unknown_tokens(self):
        with self.assertRaisesRegex(StudioError, "Unknown token"):
            update_source(self.source, {"spacing.imaginary": 4})

    def test_rejects_type_changes(self):
        with self.assertRaisesRegex(StudioError, "Wrong value type"):
            update_source(self.source, {"spacing.control-height": "thirty"})

    def test_rejects_non_scalar_values(self):
        with self.assertRaisesRegex(StudioError, "Wrong value type"):
            update_source(self.source, {"type.ui": ["Inter"]})

    def test_rejects_object_paths(self):
        with self.assertRaisesRegex(StudioError, "not a scalar"):
            value_at(read_tokens(self.source), "color.light")


LINKED = """\
[palette.light]
accent = "#1853C7"
blue = "#286486"

[color.light]
accent = "@palette.light.accent" # linked
focus = "@color.light.accent"
surface-raised-alpha = 0.78

[color.dark]
accent = "#1853C7"
focus = "@color.dark.accent"

[spacing]
control-height = 28

[recipe.panel]
background = "@color.{mode}.accent"
"""


class ReferenceEditingTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.source = pathlib.Path(self.directory.name) / "tokens.toml"
        self.source.write_text(LINKED)

    def tearDown(self):
        self.directory.cleanup()

    def test_literal_unlinks_a_reference(self):
        update_source(self.source, {"color.light.accent": "#FF0000"})
        self.assertIn('accent = "#FF0000" # linked', self.source.read_text())
        self.assertEqual(read_tokens(self.source)["color"]["light"]["focus"], "@color.light.accent")

    def test_reference_relinks(self):
        update_source(self.source, {"color.light.accent": "@palette.light.blue"})
        self.assertIn('accent = "@palette.light.blue" # linked', self.source.read_text())
        update_source(self.source, {"color.light.focus": "#123456"})
        update_source(self.source, {"color.light.focus": "@palette.light.accent"})
        self.assertIn('focus = "@palette.light.accent"', self.source.read_text())

    def test_recipe_fields_relink_per_mode(self):
        update_source(self.source, {"recipe.panel.background": "@color.{mode}.focus"})
        self.assertIn('background = "@color.{mode}.focus"', self.source.read_text())

    def test_literal_type_is_checked_against_the_resolved_value(self):
        with self.assertRaisesRegex(StudioError, "expected str"):
            update_source(self.source, {"color.light.focus": 12})

    def test_rejects_unknown_targets_cycles_and_type_mismatches(self):
        cases = (
            ({"color.light.accent": "@palette.light.nope"}, "Unknown token"),
            ({"color.light.accent": "@color.light.focus"}, "cycle"),
            ({"color.light.accent": "@palette.light"}, "is a table"),
            ({"color.light.accent": "@spacing.control-height"}, "Wrong value type"),
            ({"spacing.control-height": "@color.light.surface-raised-alpha"}, "Wrong value type"),
            ({"recipe.panel.background": "@color.{mode}.nope"}, "Unknown token"),
        )
        for changes, message in cases:
            with self.assertRaisesRegex(StudioError, message, msg=changes):
                update_source(self.source, changes)
        self.assertEqual(self.source.read_text(), LINKED)


class ExportTest(unittest.TestCase):
    """script/design-system export writes a static, read-only copy of the studio."""

    def test_exports_web_files_and_api_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "design-system" / "tokens.toml"
            source.parent.mkdir()
            source.write_text(LINKED)
            (root / "dots").mkdir()
            (root / "design-system" / "consumers.toml").write_text(
                '[[consumer]]\nname = "dots"\nroot = "dots"\nadapters = ["hunk"]\n')
            (root / "captures").mkdir()
            (root / "captures" / "bar-light.png").write_bytes(b"png")
            out = root / "site"
            env = {key: value for key, value in os.environ.items()
                   if key not in ("WHITE_PILL_DOTFILES", "WHITE_PILL_CONSUMERS")}
            result = subprocess.run(
                [sys.executable, str(ROOT / "script" / "design-system"), "--source", str(source), "export", str(out)],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((out / "index.html").is_file())
            self.assertTrue((out / "fixtures" / "browser-page.html").is_file())
            self.assertTrue((out / ".nojekyll").is_file())
            self.assertEqual((out / "captures" / "bar-light.png").read_bytes(), b"png")
            state = json.loads((out / "api" / "state.json").read_text())
            self.assertTrue(state["ok"])
            # Exported paths are repo-relative so the public snapshot does not embed the machine layout.
            self.assertEqual(state["source"], "design-system/tokens.toml")
            self.assertEqual(state["raw"]["spacing"]["control-height"], 28)
            self.assertEqual([c["name"] for c in state["manifest"]["consumers"]], ["studio", "dots"])
            listed = json.loads((out / "api" / "captures.json").read_text())
            self.assertTrue(listed["ok"])
            self.assertEqual(listed["captures"], [])


if __name__ == "__main__":
    unittest.main()
