"""Canonical token document model.

Shared by `script/design-system` and the studio server. It knows how to load
the TOML source, resolve `@section.path` references (including per-mode
`{mode}` templates inside recipes), describe the expected schema, and validate
a document. It does not know about consumers; see `adapters.py` for that.
"""

from __future__ import annotations

import fnmatch
import functools
import pathlib
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field

MODES = ("light", "dark")
REFERENCE_PREFIX = "@"
MODE_PLACEHOLDER = "{mode}"
READ_ONLY_SECTIONS = ("recipe", "meta")
HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class TokenError(Exception):
    pass


def load(source: pathlib.Path) -> dict:
    with pathlib.Path(source).open("rb") as file:
        return tomllib.load(file)


def project_root(source: pathlib.Path) -> pathlib.Path:
    """The studio repository holding a token source (`<root>/design-system/tokens.toml`)."""
    source = pathlib.Path(source)
    return source.parent.parent if source.parent.name == "design-system" else source.parent


# ── Paths ────────────────────────────────────────────────────────────────


def flatten(tree: dict, prefix: str = "") -> dict:
    """Map every scalar to its dotted path, preserving document order."""
    result = {}
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.update(flatten(value, path))
        else:
            result[path] = value
    return result


def tables(tree: dict, prefix: str = "") -> set:
    result = set()
    for key, value in tree.items():
        if isinstance(value, dict):
            path = f"{prefix}.{key}" if prefix else key
            result.add(path)
            result |= tables(value, path)
    return result


def node_at(tree: dict, path: str):
    """The value or table at a dotted path; KeyError if any segment is missing."""
    current = tree
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(path)
        current = current[part]
    return current


def is_reference(value) -> bool:
    return isinstance(value, str) and value.startswith(REFERENCE_PREFIX)


def is_recipe_path(path: str) -> bool:
    return path.startswith("recipe.")


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ── References ───────────────────────────────────────────────────────────


class ReferenceProblem(TokenError):
    """A reference that cannot be resolved.

    `path` is the token being resolved; `at` is the token whose own
    reference is broken (for a cycle, the first cycle member reached).
    Validation reports a problem only where `path == at`, so tokens that
    merely point at a broken token do not repeat the same error.
    """

    def __init__(self, path: str, message: str, at: str | None = None):
        super().__init__(message)
        self.path = path
        self.at = at or path


@dataclass
class Resolution:
    value: object
    origin: str  # canonical path of the starting token (after recipe mode overrides)
    chain: list = field(default_factory=list)  # each hop's canonical path, ending at the literal


class Resolver:
    """Resolves references in a raw token tree.

    Rules:
    - `"@a.b.c"` refers to the scalar at `a.b.c`; references resolve
      transitively and may not form cycles or point at tables.
    - `{mode}` inside a reference is replaced with the active mode. Only
      recipe fields are resolved per mode, so only they may use it.
    - `recipe.<name>.<field>` looked up for a mode first checks
      `recipe.<name>.<mode>.<field>`, so a recipe may override individual
      fields for one mode.
    - Non-recipe tokens may not reference recipes: dependencies point from
      recipes down to semantic roles and foundations, never back up.
    """

    def __init__(self, raw: dict):
        self.raw = raw
        self.flat = flatten(raw)
        self.tables = tables(raw)

    def lookup(self, path: str, mode: str | None) -> tuple[str, object]:
        parts = path.split(".")
        if mode and len(parts) == 3 and parts[0] == "recipe":
            override = f"recipe.{parts[1]}.{mode}.{parts[2]}"
            if override in self.flat:
                return override, self.flat[override]
        if path in self.flat:
            return path, self.flat[path]
        if path in self.tables:
            raise ReferenceProblem(path, f"{path} is a table, not a scalar token")
        raise ReferenceProblem(path, f"Unknown token {path}")

    @staticmethod
    def target(reference: str, mode: str | None, owner: str) -> str:
        """The path a reference points at, with `{mode}` substituted."""
        target = reference[len(REFERENCE_PREFIX):]
        if MODE_PLACEHOLDER in target:
            if mode is None:
                raise ReferenceProblem(owner, f"{owner} uses {MODE_PLACEHOLDER} outside a recipe")
            target = target.replace(MODE_PLACEHOLDER, mode)
        if "{" in target or "}" in target:
            raise ReferenceProblem(owner, f"{owner} has an unsupported placeholder in {reference}")
        if not target:
            raise ReferenceProblem(owner, f"{owner} has an empty reference")
        return target

    def resolve(self, path: str, mode: str | None = None) -> Resolution:
        origin, value = self.lookup(path, mode)
        seen = [origin]
        current = origin
        chain = []
        while is_reference(value):
            target = self.target(value, mode, current)
            if not is_recipe_path(origin) and is_recipe_path(target):
                raise ReferenceProblem(origin, f"{current} may not reference recipe {target}", current)
            try:
                canonical, value = self.lookup(target, mode)
            except ReferenceProblem as error:
                raise ReferenceProblem(origin, f"{current} → {error}", current) from None
            if canonical in seen:
                members = seen[seen.index(canonical):]
                cycle = " → ".join([*members, canonical])
                raise ReferenceProblem(origin, f"Reference cycle: {cycle}", origin if origin in members else canonical)
            seen.append(canonical)
            chain.append(canonical)
            current = canonical
        return Resolution(value, origin, chain)

    def value(self, path: str, mode: str | None = None):
        return self.resolve(path, mode).value

    # Recipes -------------------------------------------------------------

    def recipe_lookups(self, path: str) -> list:
        """(mode, path to resolve) for each mode in which a recipe field applies.

        A `recipe.<name>.<mode>.<field>` override applies to its mode only and
        resolves as `recipe.<name>.<field>`; a base field applies to every mode
        that does not override it.
        """
        parts = path.split(".")
        if len(parts) == 4 and parts[2] in MODES:
            return [(parts[2], f"recipe.{parts[1]}.{parts[3]}")]
        return [(mode, path) for mode in MODES if f"recipe.{parts[1]}.{mode}.{parts[-1]}" not in self.flat]

    def recipe_names(self) -> list:
        recipes = self.raw.get("recipe", {})
        return [name for name, value in recipes.items() if isinstance(value, dict)]

    def recipe_fields(self, name: str, mode: str) -> list:
        """Field names of a recipe for one mode (base fields, then mode-only ones)."""
        recipe = self.raw.get("recipe", {}).get(name, {})
        names = [key for key, value in recipe.items() if not isinstance(value, dict)]
        override = recipe.get(mode, {})
        if isinstance(override, dict):
            names += [key for key, value in override.items() if not isinstance(value, dict) and key not in names]
        return names


def resolved_tree(raw: dict) -> dict:
    """Replace every non-recipe reference with its resolved value.

    Recipe tables are per-mode templates and stay as written. Unresolvable
    references are left as their raw strings; validation reports them.
    """
    resolver = Resolver(raw)

    def walk(node: dict, prefix: str) -> dict:
        result = {}
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                result[key] = walk(value, path)
            elif is_reference(value) and not is_recipe_path(path):
                try:
                    result[key] = resolver.value(path)
                except ReferenceProblem:
                    result[key] = value
            else:
                result[key] = value
        return result

    return walk(raw, "")


# ── Schema ───────────────────────────────────────────────────────────────

COLOR = "color"
OPACITY = "opacity"
DIMENSION = "dimension"  # non-negative number (px or ms)
OFFSET = "offset"  # number that may be negative
NUMBER = "number"
STRING = "string"
BOOL = "bool"

# Table patterns use `{mode}` for light/dark and `*` for any single segment.
# Key patterns are fnmatch globs checked in order; the first match wins.
SCHEMA = {
    "meta": {"name": STRING, "direction": STRING, "version": NUMBER},
    "wide-gamut": {"accent": STRING},
    "palette.*": {"mode": STRING, "*": COLOR},  # free color map
    "color.{mode}": {
        "*-alpha": OPACITY,
        **dict.fromkeys((
            "canvas", "surface-raised", "surface-solid", "surface-alt",
            "text-primary", "text-secondary", "text-muted", "accent", "focus",
            "danger", "success", "warning", "info", "selection", "border-solid",
            "note-border", "diff-add", "diff-delete", "window-border-inactive",
        ), COLOR),
    },
    "control.{mode}": {
        **dict.fromkeys((
            "primary-background", "primary-foreground", "toggle-on-background",
            "toggle-on-foreground", "slider-fill", "focus-ring",
        ), COLOR),
        "selected-fill-alpha": OPACITY,
        "hover-fill-alpha": OPACITY,
    },
    "chrome.{mode}": dict.fromkeys(
        ("bar-background-alpha", "overlay-background-alpha", "scrim-alpha"), OPACITY
    ),
    "syntax.{mode}": dict.fromkeys((
        "keyword", "string", "comment", "function", "variable", "type",
        "number", "operator", "punctuation",
    ), COLOR),
    "type": {
        **dict.fromkeys(("ui", "ui-strong", "bar", "mono"), STRING),
        "base-size": DIMENSION,
        "size-*": DIMENSION,
        "weight-*": NUMBER,
        "tracking-*": NUMBER,  # em; negative tightens
    },
    "spacing": {
        "unit": DIMENSION,
        "scale": NUMBER,
        "scale-with-font": BOOL,
        **dict.fromkeys((
            "control-gap", "control-padding-x", "control-padding-y",
            "input-padding-y", "control-height", "popup-row-height", "row-gap",
            "row-padding-x", "label-gap", "popup-padding", "panel-padding",
            "panel-gap", "osd-padding-x", "osd-padding-y",
        ), DIMENSION),
    },
    "radius": dict.fromkeys(("small", "control", "surface", "full"), DIMENSION),
    "border.*": {
        "color": COLOR, "alpha": OPACITY, "width": DIMENSION,
        "inner-color": COLOR, "inner-alpha": OPACITY, "inner-width": DIMENSION,
    },
    "shadow.*": {"color": COLOR, "alpha": OPACITY, "blur": DIMENSION, "x": OFFSET, "y": OFFSET},
    "blur": {"floating": DIMENSION, "tooltip": DIMENSION},
    "window": {
        **dict.fromkeys((
            "border-width", "gaps-inner", "gaps-outer", "gaps-outer-top", "rounding", "shadow-range",
        ), DIMENSION),
        "rounding-power": NUMBER,
        "shadow-color": COLOR,
        "shadow-alpha": OPACITY,
        "shadow-alpha-inactive": OPACITY,
        "shadow-x": OFFSET,
        "shadow-y": OFFSET,
    },
    "motion": {
        "fast": DIMENSION, "standard": DIMENSION, "slow": DIMENSION,
        "easing": STRING, "theme-easing": STRING,
    },
}

# Recipe fields are free-form data; their kind follows from the field name.
RECIPE_FIELD_KINDS = (
    ("alpha", OPACITY),
    ("*-alpha", OPACITY),
    ("*-alpha-*", OPACITY),
    ("opacity-*", OPACITY),
    ("*-width", DIMENSION),
    ("*-blur", DIMENSION),
    ("shadow-x", OFFSET),
    ("shadow-y", OFFSET),
    ("size-*", DIMENSION),
    ("padding-*", DIMENSION),
    ("padding", DIMENSION),
    ("size", DIMENSION),
    ("line-height", NUMBER),  # unitless multiple of the font size
    ("*-height", DIMENSION),
    ("*-radius", DIMENSION),
    ("rounding", DIMENSION),
    ("gaps-*", DIMENSION),
    ("shadow-range", DIMENSION),
    ("*-power", NUMBER),
    ("*-size", DIMENSION),
    ("*-inset", DIMENSION),
    ("*-length", DIMENSION),
    ("gap", DIMENSION),
    ("width", DIMENSION),
    ("*-adjust", NUMBER),  # fractional adjustment, e.g. ghostty's adjust-cell-height 15% = 0.15
    ("*-scale", NUMBER),  # multiplier, e.g. the desktop's text-scaling-factor
    ("scale-with-font", BOOL),
    ("transparent", BOOL),
    ("*-family", STRING),
    ("*", COLOR),
)


def _table_matches(pattern: str, table: str) -> bool:
    parts, candidates = pattern.split("."), table.split(".")
    if len(parts) != len(candidates):
        return False
    for part, candidate in zip(parts, candidates):
        if part == "{mode}":
            if candidate not in MODES:
                return False
        elif part != "*" and part != candidate:
            return False
    return True


def token_kind(path: str) -> str | None:
    """Expected kind of a non-recipe token, or None if the schema has no such key."""
    table, _, key = path.rpartition(".")
    if key == "alpha" or key.endswith("-alpha"):
        return OPACITY
    for pattern, keys in SCHEMA.items():
        if _table_matches(pattern, table):
            for key_pattern, kind in keys.items():
                if fnmatch.fnmatchcase(key, key_pattern):
                    return kind
            return None
    return None


def recipe_field_kind(name: str) -> str:
    for pattern, kind in RECIPE_FIELD_KINDS:
        if fnmatch.fnmatchcase(name, pattern):
            return kind
    return COLOR


# ── Contrast ─────────────────────────────────────────────────────────────


def hex_rgb(value: str) -> tuple:
    if not isinstance(value, str) or not HEX_COLOR.match(value):
        raise ValueError(f"Not a #RRGGBB color: {value!r}")
    return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))


def relative_luminance(color: str | tuple) -> float:
    """WCAG 2.x relative luminance of an sRGB color.

    Channels are linearised with the sRGB transfer function (threshold
    0.04045, as in IEC 61966-2-1 and the WCAG 2.2 note) and weighted
    0.2126 R + 0.7152 G + 0.0722 B.
    """
    rgb = hex_rgb(color) if isinstance(color, str) else color

    def linear(channel: float) -> float:
        channel /= 255
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(foreground, background) -> float:
    """WCAG contrast ratio (L1 + 0.05) / (L2 + 0.05), from 1 to 21."""
    lighter, darker = sorted((relative_luminance(foreground), relative_luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def composite(color: str, alpha: float, background: str) -> tuple:
    """Source-over composite of `color` at `alpha` on an opaque background, in sRGB."""
    top, bottom = hex_rgb(color), hex_rgb(background)
    return tuple(round(alpha * a + (1 - alpha) * b) for a, b in zip(top, bottom))


# ── Validation ───────────────────────────────────────────────────────────

CONTRAST_WARNING = 4.5
CONTRAST_ERROR = 3.0


def issue(path, level, code, message) -> dict:
    return {"path": path, "level": level, "code": code, "message": message}


def _check_kind(path: str, kind: str, value, source_kind: str | None = None) -> dict | None:
    """Check a resolved value against its expected kind.

    `source_kind` is the kind of the literal a reference resolved to, or None
    for a literal. A reference to a token of the same kind is not rechecked:
    that token reports its own issues.
    """
    via_reference = source_kind is not None
    if via_reference and source_kind == kind:
        return None

    def mismatch(code, expected):
        if via_reference:
            return issue(path, "error", "reference", f"{path} resolves to {value!r}; expected {expected}")
        return issue(path, "error", code, f"{path} must be {expected}, not {value!r}")

    if kind == COLOR:
        if not isinstance(value, str) or not HEX_COLOR.match(value):
            return mismatch("color", "a #RRGGBB color")
    elif kind == OPACITY:
        if not is_number(value):
            return mismatch("opacity", "a number from 0 to 1")
        if not 0 <= value <= 1:
            return issue(path, "error", "opacity", f"{path} must be between 0 and 1, not {value}")
    elif kind in (DIMENSION, OFFSET, NUMBER):
        if not is_number(value):
            return mismatch("dimension", "a number")
        if kind == DIMENSION and value < 0:
            return issue(path, "error", "dimension", f"{path} must not be negative ({value})")
    elif kind == STRING and via_reference and not isinstance(value, str):
        return mismatch("reference", "a string")
    elif kind == BOOL and via_reference and not isinstance(value, bool):
        return mismatch("reference", "a boolean")
    return None


def _contrast_issue(path, label, ratio) -> dict | None:
    if ratio < CONTRAST_ERROR:
        level = "error"
    elif ratio < CONTRAST_WARNING:
        level = "warning"
    else:
        return None
    return issue(path, level, "contrast", f"{label} contrast is {ratio:.2f}:1 (minimum {CONTRAST_WARNING}:1)")


def contrast_issues(resolver: Resolver) -> list:
    issues = []

    def value(path):
        try:
            return resolver.value(path)
        except ReferenceProblem:
            return None

    for mode in MODES:
        text = value(f"color.{mode}.text-primary")
        canvas = value(f"color.{mode}.canvas")
        raised = value(f"color.{mode}.surface-raised")
        alpha = value(f"color.{mode}.surface-raised-alpha")
        try:
            if text and canvas:
                ratio = contrast_ratio(text, canvas)
                found = _contrast_issue(f"color.{mode}.text-primary", f"{mode} text-primary on canvas", ratio)
                if found:
                    issues.append(found)
                if raised and is_number(alpha) and 0 <= alpha <= 1:
                    surface = composite(raised, alpha, canvas)
                    ratio = contrast_ratio(hex_rgb(text), surface)
                    found = _contrast_issue(
                        f"color.{mode}.text-primary",
                        f"{mode} text-primary on surface-raised ({alpha:g} over canvas)", ratio,
                    )
                    if found:
                        issues.append(found)
        except ValueError:
            pass  # malformed colors are reported by the color check

        foreground = value(f"control.{mode}.primary-foreground")
        background = value(f"control.{mode}.primary-background")
        try:
            if foreground and background:
                ratio = contrast_ratio(foreground, background)
                if ratio < CONTRAST_WARNING:
                    issues.append(issue(
                        f"control.{mode}.primary-foreground", "warning", "contrast",
                        f"{mode} primary-foreground on primary-background contrast is {ratio:.2f}:1 "
                        f"(minimum {CONTRAST_WARNING}:1)",
                    ))
        except ValueError:
            pass
    return issues


def source_kind(resolution: Resolution) -> str | None:
    """Kind of the literal a reference resolved to; None if it was a literal."""
    if not resolution.chain:
        return None
    final = resolution.chain[-1]
    if is_recipe_path(final):
        return recipe_field_kind(final.rpartition(".")[2])
    return token_kind(final) or "unknown"


# ── Font families ────────────────────────────────────────────────────────
#
# Qt and Chromium ask fontconfig for a family by name. A name no installed
# font carries (e.g. "Iosevka" when only "Iosevka Nerd Font …" is installed)
# does not fail: fontconfig substitutes its default sans, and Qt then falls
# back to the desktop font, so the surface silently renders in Inter. The
# validator therefore checks every family token against `fc-match`.

FONT_TOKENS = ("type.ui", "type.ui-strong", "type.bar", "type.mono")


def fc_families(family: str) -> list | None:
    """Family names of the font fontconfig would draw for `family`.

    Returns None when fontconfig is unavailable, so callers skip the check.
    """
    if shutil.which("fc-match") is None:
        return None
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{family}", family],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return [name.strip() for name in result.stdout.split(",") if name.strip()]


@functools.lru_cache(maxsize=1)
def installed_families() -> tuple:
    """Primary family name of every installed font, sorted."""
    if shutil.which("fc-list") is None:
        return ()
    try:
        result = subprocess.run(
            ["fc-list", "-f", "%{family[0]}\n"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    return tuple(sorted({line.strip() for line in result.stdout.splitlines() if line.strip()}))


def font_families(resolver: Resolver) -> list:
    """(path, family) for the font-family tokens and literal recipe font-family fields."""
    result = []
    for path in FONT_TOKENS:
        if path in resolver.flat:
            try:
                result.append((path, resolver.value(path)))
            except ReferenceProblem:
                pass
    for path, value in resolver.flat.items():
        if (is_recipe_path(path) and path.count(".") == 2 and path.endswith("family")
                and isinstance(value, str) and not is_reference(value)):
            result.append((path, value))
    return result


def font_issues(resolver: Resolver, matcher=fc_families) -> list:
    issues = []
    for path, value in font_families(resolver):
        if not isinstance(value, str) or not value.strip():
            continue
        families = matcher(value)
        if families is None:
            return issues  # fontconfig unavailable; nothing to check against
        if value.lower() in {name.lower() for name in families}:
            continue
        drawn = families[0] if families else "the default font"
        first_word = value.split()[0].lower()
        suggestions = [name for name in installed_families() if first_word in name.lower()][:6]
        message = f"{path} = {value!r} is not an installed font family; fontconfig would draw {drawn!r}"
        if suggestions:
            message += f". Installed: {', '.join(suggestions)}"
        issues.append(issue(path, "error", "font", message))
    return issues


def validate(raw: dict, font_matcher=fc_families) -> list:
    """Return validation issues for a raw token tree, errors first."""
    resolver = Resolver(raw)
    issues = []
    seen = set()

    def add(found):
        if found:
            key = (found["path"], found["code"], found["message"])
            if key not in seen:
                seen.add(key)
                issues.append(found)

    for path, raw_value in resolver.flat.items():
        if is_recipe_path(path):
            continue
        kind = token_kind(path)
        if kind is None:
            add(issue(path, "warning", "unknown-key", f"{path} is not part of the token schema"))
        try:
            resolution = resolver.resolve(path)
        except ReferenceProblem as error:
            if error.at == path:
                add(issue(path, "error", "reference", str(error)))
            continue
        if kind is not None:
            add(_check_kind(path, kind, resolution.value, source_kind(resolution)))

    recipes = raw.get("recipe", {})
    if not isinstance(recipes, dict):
        add(issue("recipe", "error", "unknown-key", "recipe must be a table of recipes"))
        recipes = {}
    for name, recipe in recipes.items():
        if not isinstance(recipe, dict):
            add(issue(f"recipe.{name}", "warning", "unknown-key", f"recipe.{name} must be a table"))
            continue
        for key, value in recipe.items():
            if isinstance(value, dict) and key not in MODES:
                add(issue(f"recipe.{name}.{key}", "warning", "unknown-key",
                          f"recipe.{name}.{key}: mode overrides must be named {' or '.join(MODES)}"))
            elif isinstance(value, dict):
                for inner, inner_value in value.items():
                    if isinstance(inner_value, dict):
                        add(issue(f"recipe.{name}.{key}.{inner}", "warning", "unknown-key",
                                  f"recipe.{name}.{key}.{inner}: recipes nest at most one mode table"))
        for mode in MODES:
            for field_name in resolver.recipe_fields(name, mode):
                path = f"recipe.{name}.{field_name}"
                try:
                    resolution = resolver.resolve(path, mode)
                except ReferenceProblem as error:
                    if error.at == error.path:
                        add(issue(error.path, "error", "reference", f"{error} ({mode})"))
                    continue
                add(_check_kind(resolution.origin, recipe_field_kind(field_name), resolution.value,
                                source_kind(resolution)))

    for found in contrast_issues(resolver):
        add(found)
    for found in font_issues(resolver, font_matcher):
        add(found)
    return sorted(issues, key=lambda item: item["level"] != "error")


def errors(issues: list) -> list:
    return [item for item in issues if item["level"] == "error"]


def format_issue(item: dict) -> str:
    return f"{item['level']:<7} {item['code']:<11} {item['path'] or '-'}: {item['message']}"
