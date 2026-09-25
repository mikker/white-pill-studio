import pathlib
import sys
import tomllib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import tokens_model
from tokens_model import ReferenceProblem, Resolver, contrast_ratio, relative_luminance, resolved_tree, validate

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "tokens.toml"

DOCUMENT = """\
[palette.light]
accent = "#1853C7"
background = "#F0EDEC"

[palette.dark]
accent = "#1853C7"
background = "#1C1917"

[color.light]
canvas = "@palette.light.background"
accent = "@palette.light.accent"
focus = "@color.light.accent"
text-primary = "#2C363C"

[color.dark]
canvas = "@palette.dark.background"
accent = "@palette.dark.accent"
focus = "@color.dark.accent"
text-primary = "#B4BDC3"

[border.glass]
color = "#FFFFFF"

[border.glass-dark]
color = "#000000"

[recipe.panel]
background = "@color.{mode}.canvas"
edge-width = 1

[recipe.panel.light]
border = "@border.glass.color"

[recipe.panel.dark]
border = "@border.glass-dark.color"

[recipe.tooltip]
border = "@recipe.panel.border"
"""


def document(**overrides):
    raw = tomllib.loads(DOCUMENT)
    for path, value in overrides.items():
        *parents, key = path.split(".")
        node = raw
        for part in parents:
            node = node.setdefault(part, {})
        node[key] = value
    return raw


def codes(issues, level=None):
    return {(item["code"], item["path"]) for item in issues if level is None or item["level"] == level}


class ResolverTest(unittest.TestCase):
    def test_resolves_transitively_with_chain(self):
        resolution = Resolver(document()).resolve("color.light.focus")
        self.assertEqual(resolution.value, "#1853C7")
        self.assertEqual(resolution.chain, ["color.light.accent", "palette.light.accent"])

    def test_literal_has_empty_chain(self):
        resolution = Resolver(document()).resolve("color.light.text-primary")
        self.assertEqual((resolution.value, resolution.chain), ("#2C363C", []))

    def test_detects_cycles(self):
        raw = document(**{"color.light.accent": "@color.light.focus"})
        with self.assertRaisesRegex(ReferenceProblem, "Reference cycle"):
            Resolver(raw).resolve("color.light.focus")

    def test_detects_self_reference(self):
        raw = document(**{"color.light.accent": "@color.light.accent"})
        with self.assertRaisesRegex(ReferenceProblem, "cycle"):
            Resolver(raw).resolve("color.light.accent")

    def test_reports_unknown_targets_and_tables(self):
        with self.assertRaisesRegex(ReferenceProblem, "Unknown token palette.light.nope"):
            Resolver(document(**{"color.light.accent": "@palette.light.nope"})).resolve("color.light.accent")
        with self.assertRaisesRegex(ReferenceProblem, "is a table"):
            Resolver(document(**{"color.light.accent": "@palette.light"})).resolve("color.light.accent")

    def test_recipes_substitute_mode_and_honour_mode_overrides(self):
        resolver = Resolver(document())
        self.assertEqual(resolver.value("recipe.panel.background", "light"), "#F0EDEC")
        self.assertEqual(resolver.value("recipe.panel.background", "dark"), "#1C1917")
        dark = resolver.resolve("recipe.tooltip.border", "dark")
        self.assertEqual(dark.value, "#000000")
        self.assertEqual(dark.chain, ["recipe.panel.dark.border", "border.glass-dark.color"])
        self.assertEqual(resolver.recipe_fields("panel", "light"), ["background", "edge-width", "border"])

    def test_mode_placeholder_requires_a_mode(self):
        with self.assertRaisesRegex(ReferenceProblem, "outside a recipe"):
            Resolver(document()).resolve("recipe.panel.background")

    def test_resolved_tree_keeps_shape_and_recipe_templates(self):
        tree = resolved_tree(document())
        self.assertEqual(tree["color"]["light"]["focus"], "#1853C7")
        self.assertEqual(tree["recipe"]["panel"]["background"], "@color.{mode}.canvas")


class ValidationTest(unittest.TestCase):
    def test_clean_document_has_no_issues(self):
        self.assertEqual(validate(document()), [])

    def test_canonical_fixture_is_clean(self):
        self.assertEqual(validate(tokens_model.load(FIXTURE), font_matcher=lambda family: [family]), [])

    def test_color(self):
        issues = validate(document(**{"color.light.text-primary": "#333"}))
        self.assertIn(("color", "color.light.text-primary"), codes(issues, "error"))
        issues = validate(document(**{"recipe.panel.edge": "blue"}))
        self.assertIn(("color", "recipe.panel.edge"), codes(issues, "error"))

    def test_opacity(self):
        for value in (1.5, -0.1, "0.5"):
            issues = validate(document(**{"color.light.surface-raised-alpha": value}))
            self.assertIn(("opacity", "color.light.surface-raised-alpha"), codes(issues, "error"), value)
        self.assertEqual(validate(document(**{"color.light.surface-raised-alpha": 1})), [])

    def test_dimension(self):
        issues = validate(document(**{"spacing.control-height": -1, "radius.small": "4px", "shadow.low.y": -2}))
        self.assertIn(("dimension", "spacing.control-height"), codes(issues, "error"))
        self.assertIn(("dimension", "radius.small"), codes(issues, "error"))
        self.assertNotIn(("dimension", "shadow.low.y"), codes(issues))
        issues = validate(document(**{"recipe.panel.edge-width": -1}))
        self.assertIn(("dimension", "recipe.panel.edge-width"), codes(issues, "error"))

    def test_contrast(self):
        warning = validate(document(**{"color.light.text-primary": "#7A7A7A"}))
        self.assertIn(("contrast", "color.light.text-primary"), codes(warning, "warning"))
        error = validate(document(**{"color.light.text-primary": "#C8C8C8"}))
        self.assertIn(("contrast", "color.light.text-primary"), codes(error, "error"))
        surface = validate(document(**{
            "color.dark.surface-raised": "#B4BDC3", "color.dark.surface-raised-alpha": 0.9,
        }))
        self.assertTrue(any("surface-raised" in item["message"] for item in surface if item["code"] == "contrast"))
        control = validate(document(**{
            "control.light.primary-foreground": "#FFFFFF", "control.light.primary-background": "#8CA0D0",
        }))
        self.assertIn(("contrast", "control.light.primary-foreground"), codes(control, "warning"))

    def test_unknown_keys(self):
        issues = validate(document(**{"spacing.gutter": 4, "palette.light.anything": "#123456", "recipe.panel.hover.edge": "#FFFFFF"}))
        self.assertIn(("unknown-key", "spacing.gutter"), codes(issues, "warning"))
        self.assertIn(("unknown-key", "recipe.panel.hover"), codes(issues, "warning"))
        self.assertNotIn(("unknown-key", "palette.light.anything"), codes(issues))

    def test_references(self):
        issues = validate(document(**{"color.light.accent": "@color.light.focus", "color.dark.canvas": "@nope.x"}))
        errors = codes(issues, "error")
        self.assertIn(("reference", "color.light.accent"), errors)
        self.assertIn(("reference", "color.light.focus"), errors)
        self.assertIn(("reference", "color.dark.canvas"), errors)
        # tokens that merely point at a broken token do not repeat the error
        self.assertNotIn(("reference", "recipe.panel.background"), errors)

    def test_reference_type_mismatch(self):
        issues = validate(document(**{"type.ui": "Inter", "color.light.text-primary": "@type.ui"}))
        self.assertIn(("reference", "color.light.text-primary"), codes(issues, "error"))
        issues = validate(document(**{"spacing.unit": 2, "recipe.panel.background": "@spacing.unit"}))
        self.assertIn(("reference", "recipe.panel.background"), codes(issues, "error"))

    def test_tokens_may_not_reference_recipes_or_use_mode(self):
        issues = validate(document(**{"color.light.accent": "@recipe.panel.edge-width", "color.dark.accent": "@color.{mode}.canvas"}))
        self.assertIn(("reference", "color.light.accent"), codes(issues, "error"))
        self.assertIn(("reference", "color.dark.accent"), codes(issues, "error"))


class ContrastTest(unittest.TestCase):
    def test_wcag_reference_values(self):
        self.assertAlmostEqual(relative_luminance("#FFFFFF"), 1.0)
        self.assertAlmostEqual(relative_luminance("#000000"), 0.0)
        self.assertAlmostEqual(contrast_ratio("#000000", "#FFFFFF"), 21.0)
        self.assertAlmostEqual(contrast_ratio("#777777", "#FFFFFF"), 4.48, places=2)
        self.assertEqual(contrast_ratio("#FFFFFF", "#1853C7"), contrast_ratio("#1853C7", "#FFFFFF"))

    def test_composite(self):
        self.assertEqual(tokens_model.composite("#FFFFFF", 0.5, "#000000"), (128, 128, 128))


if __name__ == "__main__":
    unittest.main()


CANONICAL = pathlib.Path(__file__).resolve().parents[1] / "design-system" / "tokens.toml"


class WindowTokensTest(unittest.TestCase):
    """Window, terminal, and browser tokens mirror the real consumers."""

    @classmethod
    def setUpClass(cls):
        cls.raw = tokens_model.load(CANONICAL)
        cls.resolver = Resolver(cls.raw)

    def recipe(self, path, mode):
        return self.resolver.value(f"recipe.{path}", mode)

    def test_canonical_source_is_clean(self):
        self.assertEqual(validate(self.raw), [])

    def test_schema_knows_the_new_tokens(self):
        kinds = {
            "window.border-width": tokens_model.DIMENSION, "window.gaps-inner": tokens_model.DIMENSION,
            "window.rounding": tokens_model.DIMENSION, "window.rounding-power": tokens_model.NUMBER,
            "window.shadow-color": tokens_model.COLOR, "window.shadow-alpha-inactive": tokens_model.OPACITY,
            "window.shadow-y": tokens_model.OFFSET, "color.dark.window-border-inactive": tokens_model.COLOR,
            "color.light.window-border-inactive-alpha": tokens_model.OPACITY,
        }
        for path, kind in kinds.items():
            self.assertEqual(tokens_model.token_kind(path), kind, path)
        fields = {
            "opacity-active": tokens_model.OPACITY, "shadow-alpha-inactive": tokens_model.OPACITY,
            "inactive-border-alpha": tokens_model.OPACITY, "rounding": tokens_model.DIMENSION,
            "gaps-outer": tokens_model.DIMENSION, "shadow-range": tokens_model.DIMENSION,
            "size": tokens_model.DIMENSION, "padding": tokens_model.DIMENSION,
            "toolbar-height": tokens_model.DIMENSION, "omnibox-radius": tokens_model.DIMENSION,
            "tab-width": tokens_model.DIMENSION, "rounding-power": tokens_model.NUMBER,
            "line-height": tokens_model.NUMBER, "font-family": tokens_model.STRING,
            "ansi-12": tokens_model.COLOR, "frame": tokens_model.COLOR, "active-border": tokens_model.COLOR,
        }
        for name, kind in fields.items():
            self.assertEqual(tokens_model.recipe_field_kind(name), kind, name)

    def test_recipe_kinds_are_validated(self):
        issues = validate(document(**{"recipe.window.opacity-active": 1.5, "recipe.window.gaps-outer": -1,
                                      "recipe.terminal.ansi-3": "yellow"}))
        errors = codes(issues, "error")
        self.assertIn(("opacity", "recipe.window.opacity-active"), errors)
        self.assertIn(("dimension", "recipe.window.gaps-outer"), errors)
        self.assertIn(("color", "recipe.terminal.ansi-3"), errors)

    def test_window_recipe_drives_hyprland(self):
        for mode in tokens_model.MODES:
            values = {field: self.recipe(f"window.{field}", mode) for field in (
                "border-width", "gaps-inner", "gaps-outer", "gaps-outer-top", "rounding", "rounding-power",
                "active-border", "inactive-border", "inactive-border-alpha", "shadow-color", "shadow-alpha",
                "shadow-alpha-inactive", "shadow-range", "shadow-x", "shadow-y",
            )}
            self.assertEqual(values, {
                "border-width": 4, "gaps-inner": 2, "gaps-outer": 12, "gaps-outer-top": 4, "rounding": 14,
                "rounding-power": 2, "active-border": "#1853C7", "inactive-border": "#FFFFFF",
                "inactive-border-alpha": 0.133, "shadow-color": "#10131A", "shadow-alpha": 0.12,
                "shadow-alpha-inactive": 0.12, "shadow-range": 18, "shadow-x": 0, "shadow-y": 4,
            }, mode)
            # rgba(ffffff22) and rgba(10131a1f) in the generated hyprland_window.lua
            self.assertEqual(round(values["inactive-border-alpha"] * 255), 0x22)
            self.assertEqual(round(values["shadow-alpha"] * 255), 0x1F)
        self.assertEqual(self.resolver.resolve("window.rounding").chain, ["radius.surface"])
        self.assertEqual(self.resolver.resolve("window.shadow-alpha").chain, ["shadow.floating.alpha"])
        # Omarchy's default-opacity rule in light; the dark theme makes windows opaque.
        self.assertEqual((self.recipe("window.opacity-active", "light"), self.recipe("window.opacity-inactive", "light")),
                         (0.985, 0.96))
        self.assertEqual((self.recipe("window.opacity-active", "dark"), self.recipe("window.opacity-inactive", "dark")),
                         (1.0, 1.0))

    def test_terminal_ansi_slots_follow_the_ghostty_template(self):
        # Omarchy's ghostty.conf.tpl: palette = N={{ key }}
        slots = ("background", "red", "green", "yellow", "blue", "magenta", "cyan", "foreground",
                 "muted", "bright_red", "bright_green", "bright_yellow", "bright_blue", "bright_magenta",
                 "bright_cyan", "bright_foreground")
        for mode in tokens_model.MODES:
            palette = self.raw["palette"][mode]
            for index, key in enumerate(slots):
                self.assertEqual(self.recipe(f"terminal.ansi-{index}", mode), palette[key], (mode, index))
            self.assertEqual(self.recipe("terminal.selection", mode), palette["selection"])
            self.assertEqual(self.recipe("terminal.selection-foreground", mode), palette["bright_foreground"])
            # The terminal family follows type.mono rather than a fixed name.
            self.assertEqual(self.recipe("terminal.font-family", mode), self.raw["type"]["mono"])

    def test_terminal_records_the_users_effective_ghostty_config(self):
        # ~/.dotfiles/ghostty/linux.config: 12pt, padding 6 / 3,6pt; the shared config's
        # adjust-cell-height 15% and cursor #dd0455 / #ffffff. The grid and offsets are what the
        # native capture measures at the desktop's 1.1818 text scale (9.5 × 27.5 cells, 9 / 4.5 px).
        for mode in tokens_model.MODES:
            self.assertEqual((self.recipe("terminal.cursor", mode), self.recipe("terminal.cursor-text", mode)),
                             ("#DD0455", "#FFFFFF"))
            self.assertEqual((self.recipe("terminal.font-size", mode), self.recipe("terminal.font-scale", mode),
                              self.recipe("terminal.cell-height-adjust", mode)), (12, 1.1818, 0.15))
            self.assertEqual((self.recipe("terminal.cell-width", mode), self.recipe("terminal.cell-height", mode)),
                             (9.5, 27.5))
            self.assertEqual((self.recipe("terminal.padding-x", mode), self.recipe("terminal.padding-top", mode)),
                             (9, 4.5))

    def test_browser_seed_is_the_chromium_theme_color(self):
        # Omarchy's chromium.theme.tpl is {{ background_rgb }}; Chromium derives the chrome from it.
        measured = {"light": ("#FFFFFF", "#E1E1E2", "#E2DDDA"), "dark": ("#443C37", "#2E2B2B", "#1C1917")}
        for mode in tokens_model.MODES:
            self.assertEqual(self.recipe("browser.seed", mode), self.raw["palette"][mode]["background"])
            self.assertEqual((self.recipe("browser.frame", mode), self.recipe("browser.omnibox", mode),
                              self.recipe("browser.new-tab-background", mode)), measured[mode])
            self.assertEqual(self.recipe("browser.active-tab", mode), self.recipe("browser.frame", mode))
            self.assertEqual(self.recipe("browser.toolbar", mode), self.recipe("browser.frame", mode))
            self.assertEqual((self.recipe("browser.opacity-active", mode), self.recipe("browser.opacity-inactive", mode)),
                             (1.0, 0.985))


class FontFamilyValidationTest(unittest.TestCase):
    INSTALLED = {
        "Inter": ["Inter"],
        "Inter Medium Tabular": ["Inter", "Inter Medium", "Inter Medium Tabular"],
        "Iosevka Nerd Font Mono": ["Iosevka Nerd Font Mono", "Iosevka NFM"],
    }

    def matcher(self, family):
        return self.INSTALLED.get(family, ["Liberation Sans"])

    def tokens(self, **families):
        raw = tokens_model.load(FIXTURE)
        raw["type"].update(families)
        return raw

    def test_installed_families_pass(self):
        raw = self.tokens(ui="Inter", bar="Inter Medium Tabular", mono="Iosevka Nerd Font Mono")
        self.assertEqual([i for i in validate(raw, font_matcher=self.matcher) if i["code"] == "font"], [])

    def test_alias_listed_by_fontconfig_passes(self):
        raw = self.tokens(bar="Inter Medium Tabular")
        self.assertEqual([i for i in validate(raw, font_matcher=self.matcher) if i["code"] == "font"], [])

    def test_unknown_family_is_an_error_naming_the_fallback(self):
        raw = self.tokens(ui="Iosevka")
        found = [i for i in validate(raw, font_matcher=self.matcher) if i["code"] == "font"]
        self.assertEqual([i["path"] for i in found], ["type.ui"])
        self.assertEqual(found[0]["level"], "error")
        self.assertIn("'Liberation Sans'", found[0]["message"])

    def test_literal_recipe_family_is_checked_but_references_are_not(self):
        raw = self.tokens(ui="Inter")
        raw.setdefault("recipe", {}).setdefault("terminal", {})["font-family"] = "Nope Sans"
        raw["recipe"].setdefault("bar", {})["font-family"] = "@type.ui"
        found = [i["path"] for i in validate(raw, font_matcher=self.matcher) if i["code"] == "font"]
        self.assertEqual(found, ["recipe.terminal.font-family"])

    def test_skips_when_fontconfig_is_unavailable(self):
        raw = self.tokens(ui="Iosevka")
        self.assertEqual([i for i in validate(raw, font_matcher=lambda family: None) if i["code"] == "font"], [])
