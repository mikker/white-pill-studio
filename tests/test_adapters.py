import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import adapters
import tokens_model

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
EXPECTED = FIXTURES / "expected"


def consumers(studio="/studio", dotfiles="/dotfiles"):
    """The studio plus a dotfiles checkout that receives every adapter family."""
    return [
        adapters.Consumer("studio", pathlib.Path(studio), ("studio-css",), "studio"),
        adapters.Consumer("dotfiles", pathlib.Path(dotfiles), adapters.FAMILIES, "default"),
    ]


class GenerationTest(unittest.TestCase):
    """The fixture TOML must generate the checked-in snapshot byte for byte.

    The snapshot was captured from the generator before references and
    recipes existed, so this guards the refactor and every future one.
    """

    def test_outputs_match_the_snapshot(self):
        raw = tokens_model.load(FIXTURES / "tokens.toml")
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "studio"
            dotfiles = pathlib.Path(directory) / "dotfiles"
            outputs = adapters.outputs(raw, consumers(root, dotfiles))
            expected = {
                path.relative_to(EXPECTED): path.read_text()
                for path in EXPECTED.rglob("*") if path.is_file()
            }
            actual = {path.relative_to(directory): content for path, content in outputs.items()}
            self.assertEqual(sorted(actual), sorted(expected))
            for path, content in expected.items():
                self.assertEqual(actual[path], content, str(path))

    def test_unresolvable_recipe_fields_stop_generation(self):
        raw = tokens_model.load(FIXTURES / "tokens.toml")
        del raw["recipe"]["window"]
        with self.assertRaisesRegex(tokens_model.TokenError, "shell.hyprland.toml"):
            adapters.outputs(raw, consumers("/r", "/d"))


class ManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = tokens_model.load(FIXTURES / "tokens.toml")
        cls.manifest = adapters.manifest(cls.raw, pathlib.Path("/studio"), consumers("/studio", "/dotfiles"))
        cls.by_display = {target["display"]: target for target in cls.manifest["targets"]}

    def test_lists_every_generated_target(self):
        outputs = adapters.outputs(self.raw, consumers("/studio", "/dotfiles"))
        self.assertEqual([target["path"] for target in self.manifest["targets"]], [str(path) for path in outputs])
        for target in self.manifest["targets"]:
            self.assertTrue(pathlib.Path(target["path"]).is_absolute())
            self.assertIn(target["family"], {"omarchy-theme", "omarchy-plugin", "hyprland", "hunk", "studio-css"})
            self.assertEqual(target["consumer"], target["display"].split(":", 1)[0])
            self.assertIn(target["consumer"], {"studio", "dotfiles"})
            self.assertTrue(target["path"].startswith(target["root"]))
            self.assertIn(target["mode"], {"light", "dark", None})
        json.dumps(self.manifest)

    def test_recipe_fields_carry_provenance(self):
        popups = self.by_display["dotfiles:omarchy-themes/mekanikos/shell.popups.toml"]
        self.assertEqual(popups["mode"], "dark")
        background = popups["fields"][0]
        self.assertEqual(
            {key: background[key] for key in ("section", "key", "recipe", "recipeField", "token", "value")},
            {"section": "popups", "key": "background", "recipe": "panel", "recipeField": "background",
             "token": "color.dark.surface-raised", "value": "#25211F"},
        )
        tooltip = self.by_display["dotfiles:omarchy-themes/mekanikos-light/shell.tooltip.toml"]
        border = next(field for field in tooltip["fields"] if field["key"] == "border")
        self.assertEqual(border["token"], "border.glass.color")
        self.assertEqual(border["chain"], ["recipe.tooltip.border", "recipe.panel.light.border", "border.glass.color"])

    def test_literals_and_css_and_qml(self):
        hunk = self.by_display["dotfiles:hunk/config.light.toml"]
        theme = hunk["fields"][0]
        self.assertEqual((theme["section"], theme["key"], theme["recipe"], theme["token"], theme["value"]),
                         (None, "theme", None, None, "custom"))
        css = self.by_display["studio:design-system/tokens.css"]
        accent = next(field for field in css["fields"] if field["section"] == "light" and field["key"] == "--color-accent")
        self.assertEqual((accent["token"], accent["value"]), ("color.light.accent", "#1853C7"))
        qml = self.by_display["dotfiles:omarchy/plugins/mikker.osd/DesignTokens.js"]
        self.assertEqual((qml["family"], qml["consumer"], qml["fields"]), ("omarchy-plugin", "dotfiles", []))

    def test_manifest_tolerates_incomplete_documents(self):
        manifest = adapters.manifest({"spacing": {"control-height": 28}}, pathlib.Path("/s"), consumers("/s", "/d"))
        bar = next(t for t in manifest["targets"] if t["display"].endswith("mekanikos/shell.bar.toml"))
        self.assertIsNone(bar["fields"][0]["value"])

    def test_cli_prints_the_manifest(self):
        env = {**os.environ, "WHITE_PILL_DOTFILES": "/tmp/white-pill-dotfiles"}
        result = subprocess.run(
            [sys.executable, str(ROOT / "script" / "design-system"), "manifest"],
            capture_output=True, text=True, env=env, check=True,
        )
        manifest = json.loads(result.stdout)
        self.assertTrue(any(t["display"] == "dotfiles:hunk/config.dark.toml" for t in manifest["targets"]))


class ProvenanceTest(unittest.TestCase):
    def test_foundation_to_consumer(self):
        raw = tokens_model.load(FIXTURES / "tokens.toml")
        manifest = adapters.manifest(raw, pathlib.Path("/studio"), consumers("/studio", "/dotfiles"))
        provenance = adapters.provenance(raw, manifest)
        focus = provenance["control.light.focus-ring"]
        self.assertEqual(focus["resolvesFrom"], "color.light.accent")
        self.assertEqual(focus["chain"], ["color.light.accent", "palette.light.accent"])
        self.assertIn("control.light.focus-ring", provenance["color.light.accent"]["referencedBy"])
        self.assertIn("recipe.window.active-border", provenance["color.light.accent"]["referencedBy"])
        used = {(entry["recipe"], entry["field"]) for entry in provenance["palette.light.accent"]["usedBy"]}
        self.assertIn(("window", "active-border"), used)
        window = next(entry for entry in provenance["color.light.accent"]["usedBy"] if entry["recipe"] == "window")
        self.assertEqual(window["consumers"], [
            {"display": "dotfiles:omarchy-themes/mekanikos-light/shell.hyprland.toml",
             "section": "hyprland", "key": "active-border"},
            *({"display": "dotfiles:omarchy-themes/mekanikos-light/hyprland_window.lua", "section": section, "key": key}
              for section, key in (("general.col", "active_border"), ("group.col", "border_active"),
                                   ("plugin.borders_plus_plus.col", "border_1"))),
        ])
        panel = provenance["recipe.panel.background"]
        self.assertEqual(panel["resolvesFrom"], "color.{mode}.surface-raised")
        self.assertEqual(panel["modes"]["dark"]["value"], "#25211F")


if __name__ == "__main__":
    unittest.main()


class WindowRecipesTest(unittest.TestCase):
    """The window/terminal/browser recipes are provenance-visible but generate nothing new yet."""

    @classmethod
    def setUpClass(cls):
        cls.raw = tokens_model.load(ROOT / "design-system" / "tokens.toml")
        cls.manifest = adapters.manifest(cls.raw, pathlib.Path("/studio"), consumers("/studio", "/dotfiles"))
        cls.provenance = adapters.provenance(cls.raw, cls.manifest)

    def test_hyprland_adapter_keeps_its_two_fields(self):
        hyprland = next(target for target in self.manifest["targets"]
                        if target["display"] == "dotfiles:omarchy-themes/mekanikos/shell.hyprland.toml")
        self.assertEqual([(field["key"], field["recipeField"]) for field in hyprland["fields"]],
                         [("active-border", "active-border"), ("active-border-foreground", "active-border-foreground")])
        self.assertFalse(any(field.get("recipe") in {"terminal", "browser"}
                             for target in self.manifest["targets"] for field in target["fields"]))

    def test_recipe_fields_carry_per_mode_provenance(self):
        ansi = self.provenance["recipe.terminal.ansi-4"]
        self.assertEqual(ansi["resolvesFrom"], "palette.{mode}.blue")
        self.assertEqual(ansi["modes"]["light"], {"chain": ["palette.light.blue"], "value": "#286486"})
        self.assertEqual(ansi["modes"]["dark"]["value"], "#6099C0")
        rounding = self.provenance["recipe.window.rounding"]
        self.assertEqual(rounding["modes"]["dark"], {"chain": ["window.rounding", "radius.surface"], "value": 14})
        inactive = self.provenance["recipe.window.inactive-border-alpha"]
        self.assertEqual(inactive["modes"]["light"]["chain"], ["color.light.window-border-inactive-alpha"])
        seed = self.provenance["recipe.browser.seed"]
        self.assertEqual(seed["modes"]["dark"], {"chain": ["palette.dark.background"], "value": "#1C1917"})
        # measured chrome tones live in the mode tables; references still resolve through them
        self.assertEqual(self.provenance["recipe.browser.dark.frame"]["modes"], {"dark": {"chain": [], "value": "#443C37"}})
        tab = self.provenance["recipe.browser.active-tab"]
        self.assertEqual(tab["modes"]["light"], {"chain": ["recipe.browser.light.frame"], "value": "#FFFFFF"})
        new_tab = self.provenance["recipe.browser.dark.new-tab-background"]
        self.assertEqual(new_tab["modes"]["dark"]["chain"], ["recipe.browser.seed", "palette.dark.background"])

    def test_foundations_list_the_recipes_that_reference_them(self):
        self.assertIn("window.rounding", self.provenance["radius.surface"]["referencedBy"])
        self.assertIn("recipe.window.rounding", self.provenance["window.rounding"]["referencedBy"])
        self.assertIn("recipe.terminal.ansi-4", self.provenance["palette.light.blue"]["referencedBy"])
        self.assertIn("recipe.terminal.font-family", self.provenance["type.mono"]["referencedBy"])
        self.assertIn("window.shadow-range", self.provenance["shadow.floating.blur"]["referencedBy"])


class HyprlandWindowAdapterTest(unittest.TestCase):
    """recipe.window → omarchy-themes/<slug>/hyprland_window.lua (the `hyprland` family)."""

    @classmethod
    def setUpClass(cls):
        cls.raw = tokens_model.load(ROOT / "design-system" / "tokens.toml")
        cls.outputs = adapters.outputs(cls.raw, consumers("/studio", "/dotfiles"))
        cls.manifest = adapters.manifest(cls.raw, pathlib.Path("/studio"), consumers("/studio", "/dotfiles"))

    def fragment(self, slug):
        return self.outputs[pathlib.Path(f"/dotfiles/omarchy-themes/{slug}/hyprland_window.lua")]

    def test_fragment_is_a_generated_lua_table(self):
        for slug in ("mekanikos", "mekanikos-light"):
            text = self.fragment(slug)
            self.assertTrue(text.startswith("-- Generated by script/design-system. Do not edit.\n"))
            self.assertIn('hl.config(require("omarchy.current.theme.hyprland_window"))', text)
            self.assertIn("\nreturn {\n", text)
            self.assertTrue(text.endswith("}\n"))
            self.assertIn("\t\tgaps_out = { top = 4, right = 12, bottom = 12, left = 12 },\n", text)
            self.assertIn('\t\t\tactive_border = "rgba(1853c7ff)",\n', text)
            self.assertIn('\t\t\tinactive_border = "rgba(ffffff22)",\n', text)
            # Hyprland reads `{ x = 0, y = 4 }` as (0, 0): vec2 tables are positional.
            self.assertIn("\t\t\toffset = { 0, 4 },\n", text)
            self.assertIn('\t\t\tcolor = "rgba(10131a1f)",\n', text)
            self.assertIn("\t\t\tborder_size_1 = 4,\n", text)

    def test_fragment_evaluates_in_lua(self):
        lua = shutil.which("lua") or shutil.which("luajit")
        if not lua:
            self.skipTest("no Lua interpreter")
        with tempfile.NamedTemporaryFile("w", suffix=".lua") as file:
            file.write(self.fragment("mekanikos-light"))
            file.flush()
            script = (f"local t = dofile({json.dumps(file.name)}) "
                      "print(t.general.border_size, t.general.gaps_out.top, t.decoration.shadow.offset[2], "
                      "t.group.col.border_active)")
            result = subprocess.run([lua, "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.split(), ["4", "4", "4", "rgba(1853c7ff)"])

    def test_manifest_fields_carry_provenance(self):
        target = next(t for t in self.manifest["targets"]
                      if t["display"] == "dotfiles:omarchy-themes/mekanikos/hyprland_window.lua")
        self.assertEqual((target["family"], target["mode"]), ("hyprland", "dark"))
        fields = {(item["section"], item["key"]): item for item in target["fields"]}
        inactive = fields[("general.col", "inactive_border")]
        self.assertEqual((inactive["recipe"], inactive["recipeField"], inactive["token"], inactive["value"]),
                         ("window", "inactive-border", "color.dark.window-border-inactive", "rgba(ffffff22)"))
        self.assertIn("color.dark.window-border-inactive-alpha", inactive["chain"])
        top = fields[("general.gaps_out", "top")]
        self.assertEqual((top["recipeField"], top["value"]), ("gaps-outer-top", 4))
        shadow = fields[("decoration.shadow", "color_inactive")]
        self.assertEqual(shadow["chain"][-1], "shadow.floating.alpha")
        provenance = adapters.provenance(self.raw, self.manifest)
        used = [consumer["display"] for entry in provenance["window.gaps-outer-top"]["usedBy"]
                for consumer in entry["consumers"]]
        self.assertIn("dotfiles:omarchy-themes/mekanikos-light/hyprland_window.lua", used)

    def test_rgba_formatting(self):
        self.assertEqual(adapters.hypr_rgba("#10131A", 0.12), "rgba(10131a1f)")
        self.assertEqual(adapters.hypr_rgba("#FFFFFF", 0.133), "rgba(ffffff22)")
        self.assertEqual(adapters.hypr_rgba("#1853C7", 1.0), "rgba(1853c7ff)")
        with self.assertRaises(tokens_model.TokenError):
            adapters.hypr_rgba("#FFF", 1)
        with self.assertRaises(tokens_model.TokenError):
            adapters.hypr_rgba("#FFFFFF", 1.5)

    def test_family_is_configurable_per_consumer(self):
        self.assertIn("hyprland", adapters.FAMILIES)
        consumer = adapters.Consumer("dots", pathlib.Path("/dots"), ("hyprland",))
        paths = [str(target.path) for target in adapters.targets([consumer])]
        self.assertEqual(paths, ["/dots/omarchy-themes/mekanikos/hyprland_window.lua",
                                 "/dots/omarchy-themes/mekanikos-light/hyprland_window.lua"])


class BarRecipeTest(unittest.TestCase):
    def test_transparent_bar_fields_validate(self):
        raw = tokens_model.load(ROOT / "design-system" / "tokens.toml")
        resolver = tokens_model.Resolver(raw)
        self.assertIs(resolver.value("recipe.bar.transparent", "light"), True)
        self.assertEqual(resolver.value("recipe.bar.transparent-text", "dark"), "#1C1917")
        self.assertEqual(resolver.value("recipe.bar.font-size", "light"), 14)
        self.assertEqual(tokens_model.recipe_field_kind("transparent"), tokens_model.BOOL)
        self.assertEqual(tokens_model.recipe_field_kind("font-size"), tokens_model.DIMENSION)
        self.assertEqual(tokens_model.token_kind("window.gaps-outer-top"), tokens_model.DIMENSION)
        raw["recipe"]["bar"]["transparent"] = "@color.light.canvas"
        self.assertTrue(any(item["path"] == "recipe.bar.transparent" for item in tokens_model.validate(raw)))
