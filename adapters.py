"""Consumer adapters: boring tables from recipes to generated files.

Each generated file is a `Target`. TOML targets are lists of blocks; each
block is an optional `[section]` header and rows of `(consumer key, source)`
or `(consumer key, source, format)`. A source is one of:

- `"recipe.field"` — a field of a `[recipe.<name>]` table, resolved per mode;
- `"@token.path"`  — a token directly (`{mode}` is substituted), used for
  foundation pass-through such as fonts and spacing;
- `Literal(value)` — consumer configuration that is not a design decision,
  optionally per mode, e.g. Hunk's `theme = "custom"` header;
- `RGBA(color, alpha)` — two sources written as Hyprland's `rgba(rrggbbaa)`.

Lua targets (the `hyprland` family) use the same blocks with dotted table
paths as sections and render one `return { ... }` table.

Formats only preserve each consumer file's established number style:
`"compact"` writes whole floats as integers, `"0.00"` writes two decimals.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import tomllib
from dataclasses import dataclass, field

from tokens_model import (
    HEX_COLOR, MODES, ReferenceProblem, Resolver, TokenError, is_number, is_recipe_path, is_reference, node_at,
)


@dataclass(frozen=True)
class Literal:
    value: object  # a scalar, or {"light": ..., "dark": ...}

    def for_mode(self, mode):
        if isinstance(self.value, dict):
            return self.value[mode]
        return self.value


# Expand every key of a free-form token table (except `exclude`) as a row.
@dataclass(frozen=True)
class Passthrough:
    table: str
    exclude: tuple = ()


# A Hyprland color, `rgba(rrggbbaa)`, from a color source and an alpha source.
# Formatting only: both halves are ordinary sources and carry provenance.
@dataclass(frozen=True)
class RGBA:
    color: object
    alpha: object


def rows(recipe: str, keys: tuple) -> list:
    return [(key, f"{recipe}.{key}") for key in keys]


SURFACE = (
    "background", "background-alpha", "text",
    "border", "border-alpha", "border-width",
    "inner-border", "inner-border-alpha", "inner-border-width",
    "shadow-color", "shadow-alpha", "shadow-blur", "shadow-x", "shadow-y",
    "backdrop-blur",
)

OVERLAY = (
    "background", "background-alpha", "text", "border", "border-alpha",
    "border-width", "scrim", "scrim-alpha", "selected-background",
    "selected-background-alpha", "selected-text", "selected-border",
    "selected-border-alpha",
)

SYNTAX = (
    "keyword", "string", "comment", "function", "variable", "type", "number",
    "operator", "punctuation",
)

SPACING = (
    "scale", "scale-with-font", "control-gap", "control-padding-x",
    "control-padding-y", "input-padding-y", "control-height",
    "popup-row-height", "row-gap", "row-padding-x", "label-gap",
    "popup-padding", "panel-padding", "panel-gap",
)

# Omarchy theme directory files, per mode: file name → blocks.
OMARCHY_THEME = {
    "colors.toml": [
        (None, [("mode", "@palette.{mode}.mode")]),
        (None, Passthrough("palette.{mode}", exclude=("mode",))),
    ],
    "shell.bar.toml": [
        ("bar", rows("bar", (
            "background", "background-alpha", "text", "active",
            "scale-with-font", "size-horizontal", "size-vertical",
        ))),
    ],
    "shell.hyprland.toml": [
        ("hyprland", rows("window", ("active-border", "active-border-foreground"))),
    ],
    "shell.font.toml": [
        ("font", [
            ("base-size", "@type.base-size"),
            ("caption", "@type.size-small"),
            ("body", "@type.size-body"),
            ("title", "@type.size-title"),
            ("ui-family", "@type.ui"),
            ("ui-strong-family", "@type.ui-strong"),
            ("bar-family", "bar.font-family"),
            ("icon-family", "@type.mono"),
            # Popup typography roles; Style ignores these keys, plugins read them.
            ("title-weight", "@type.weight-semibold"),
            ("title-tracking", "@type.tracking-title"),
            ("subtitle-weight", "@type.weight-medium"),
        ]),
    ],
    "shell.spacing.toml": [
        ("spacing", [
            *[(key, f"@spacing.{key}", "compact") for key in SPACING],
            ("osd-padding-x", "osd.padding-x", "compact"),
            ("osd-padding-y", "osd.padding-y", "compact"),
        ]),
    ],
    "shell.controls.toml": [
        ("controls", [
            ("normal-color", "controls.normal-color"),
            ("normal-fill-alpha", "controls.normal-fill-alpha"),
            ("normal-border", "controls.normal-border"),
            ("normal-border-width", "controls.normal-border-width"),
            ("normal-border-alpha", "controls.normal-border-alpha", "0.00"),
            ("hover-cursor-color", "controls.hover-cursor-color"),
            ("hover-cursor-fill-alpha", "controls.hover-cursor-fill-alpha"),
            ("hover-cursor-border", "controls.hover-cursor-border"),
            ("hover-cursor-border-width", "controls.hover-cursor-border-width"),
            ("hover-cursor-border-alpha", "controls.hover-cursor-border-alpha"),
            ("focus-color", "controls.focus-color"),
            ("focus-fill-alpha", "controls.focus-fill-alpha", "0.00"),
            ("focus-border", "controls.focus-border"),
            ("focus-border-width", "controls.focus-border-width"),
            ("focus-border-alpha", "controls.focus-border-alpha"),
            ("selected-color", "controls.selected-color"),
            ("selected-fill-alpha", "controls.selected-fill-alpha"),
            ("selected-border", "controls.selected-border"),
            ("selected-border-width", "controls.selected-border-width"),
            ("selected-border-alpha", "controls.selected-border-alpha"),
            ("pressed-fill-alpha", "controls.pressed-fill-alpha"),
            ("selection-fill-alpha", "controls.selection-fill-alpha"),
        ]),
    ],
    "shell.launcher.toml": [("launcher", rows("overlay", OVERLAY))],
    "shell.menu.toml": [("menu", rows("overlay", OVERLAY))],
    "shell.popups.toml": [("popups", rows("panel", (*SURFACE, "text-secondary")))],
    "shell.notifications.toml": [("notifications", rows("notification", (*SURFACE, "text-secondary", "width",
                                                                          "countdown")))],
    # mikker.osd; Omarchy merges any shell.<section>.toml into shell.toml.
    "shell.osd.toml": [("osd", rows("osd", (
        "icon-size", "gap", "track", "track-alpha", "track-height", "track-length", "track-radius", "fill",
    )))],
    "shell.tooltip.toml": [("tooltip", rows("tooltip", SURFACE))],
}

HUNK = [
    (None, [
        ("theme", Literal("custom")),
        ("mode", Literal("auto")),
        ("watch", Literal(False)),
        ("exclude_untracked", Literal(False)),
        ("line_numbers", Literal(True)),
        ("wrap_lines", Literal(False)),
        ("hunk_headers", Literal(True)),
        ("agent_notes", Literal(True)),
    ]),
    ("custom_theme", [
        ("base", Literal({"light": "paper", "dark": "graphite"})),
        ("label", Literal({"light": "Mekanikos Light", "dark": "Mekanikos Dark"})),
        ("accent", "code-review.accent"),
        ("panel", "code-review.panel"),
        ("panelAlt", "code-review.panel-alt"),
        ("text", "code-review.text"),
        ("muted", "code-review.muted"),
        ("border", "code-review.border"),
        ("noteBorder", "code-review.note-border"),
        ("add", "code-review.add"),
        ("delete", "code-review.delete"),
        ("selection", "code-review.selection"),
    ]),
    ("custom_theme.syntax", [(key, f"code-review.syntax-{key}") for key in SYNTAX]),
]

# Hyprland window chrome, per mode: a Lua table the theme's handwritten
# hyprland.lua passes to hl.config. Sections are dotted table paths; rules,
# blur, and the rest of hyprland.lua stay handwritten.
HYPRLAND_WINDOW_FILE = "hyprland_window.lua"
HYPRLAND_WINDOW_MODULE = "omarchy.current.theme.hyprland_window"
HYPRLAND_WINDOW = [
    ("general", [
        ("border_size", "window.border-width"),
        ("gaps_in", "window.gaps-inner"),
    ]),
    ("general.gaps_out", [
        ("top", "window.gaps-outer-top"),
        ("right", "window.gaps-outer"),
        ("bottom", "window.gaps-outer"),
        ("left", "window.gaps-outer"),
    ]),
    ("general.col", [
        ("active_border", RGBA("window.active-border", Literal(1.0))),
        ("inactive_border", RGBA("window.inactive-border", "window.inactive-border-alpha")),
    ]),
    ("decoration", [
        ("rounding", "window.rounding"),
        ("rounding_power", "window.rounding-power"),
    ]),
    ("decoration.shadow", [
        ("range", "window.shadow-range"),
        ("color", RGBA("window.shadow-color", "window.shadow-alpha")),
        ("color_inactive", RGBA("window.shadow-color", "window.shadow-alpha-inactive")),
    ]),
    ("decoration.shadow.offset", [
        ("x", "window.shadow-x"),
        ("y", "window.shadow-y"),
    ]),
    ("group.col", [
        ("border_active", RGBA("window.active-border", Literal(1.0))),
        ("border_inactive", RGBA("window.inactive-border", "window.inactive-border-alpha")),
    ]),
    ("plugin.borders_plus_plus", [
        ("border_size_1", "window.border-width"),
    ]),
    ("plugin.borders_plus_plus.col", [
        ("border_1", RGBA("window.active-border", Literal(1.0))),
    ]),
]
# Small tables written on one line, as hand-written Hyprland Lua does. Vec2
# tables are positional: Hyprland reads `offset = { x = 0, y = 4 }` as (0, 0).
LUA_INLINE = ("gaps_out",)
LUA_POSITIONAL = ("offset",)

OMARCHY_THEMES = {"dark": "mekanikos", "light": "mekanikos-light"}
OMARCHY_THEME_NAMES = {"dark": "Mekanikos", "light": "Mekanikos Light"}
QML_PLUGINS = ("mikker.bar", "mikker.notifications", "mikker.osd")

# CSS overrides applied inside the Display P3 @supports/@media block.
P3_OVERRIDES = (
    "color-accent", "color-focus", "control-primary-background",
    "control-toggle-on-background", "control-slider-fill", "control-focus-ring",
)


# ── Consumers ────────────────────────────────────────────────────────────

FAMILIES = ("omarchy-theme", "omarchy-plugin", "hyprland", "hunk")
STUDIO = "studio"  # the implicit consumer: this repository's own tokens.css
CONSUMERS_ENV = "WHITE_PILL_CONSUMERS"  # alternative consumers.toml
DEFAULT_CONSUMERS = [{
    "name": "dotfiles", "root": "~/.dotfiles", "env": "WHITE_PILL_DOTFILES",
    "adapters": list(FAMILIES),
}]


class ConsumerError(TokenError):
    pass


@dataclass(frozen=True)
class Consumer:
    name: str
    root: pathlib.Path
    families: tuple
    origin: str = "config"  # "config", "default", "studio", or the env var that overrode the root

    def to_json(self) -> dict:
        return {"name": self.name, "root": str(self.root), "adapters": list(self.families), "origin": self.origin}


def consumers_path(studio_root: pathlib.Path, environ=None) -> pathlib.Path:
    environ = os.environ if environ is None else environ
    override = environ.get(CONSUMERS_ENV)
    if override:
        return pathlib.Path(override).expanduser()
    return pathlib.Path(studio_root) / "design-system" / "consumers.toml"


def parse_consumers(document: dict, environ=None, base: pathlib.Path | None = None) -> list:
    """Consumers from a parsed consumers.toml (`[[consumer]]` tables).

    Each consumer has a `name`, a `root` (`~` and `$VAR` expand; relative
    roots resolve against `base`), an optional `env` naming a variable that
    overrides the root, and the adapter families (`adapters`) it receives.
    """
    environ = os.environ if environ is None else environ
    entries = document.get("consumer", [])
    if not isinstance(entries, list):
        raise ConsumerError("consumers.toml: use [[consumer]] tables")
    result, names = [], set()
    for index, entry in enumerate(entries):
        where = f"consumers.toml: consumer #{index + 1}"
        if not isinstance(entry, dict):
            raise ConsumerError(f"{where} must be a table")
        unknown = set(entry) - {"name", "root", "env", "adapters"}
        if unknown:
            raise ConsumerError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
        name = entry.get("name")
        if not isinstance(name, str) or not name or ":" in name:
            raise ConsumerError(f"{where}: name must be a non-empty string without ':'")
        where = f"consumers.toml: consumer {name!r}"
        if name in names or name == STUDIO:
            raise ConsumerError(f"{where}: duplicate or reserved name")
        names.add(name)
        root, origin = entry.get("root"), "config"
        env = entry.get("env")
        if env is not None and not isinstance(env, str):
            raise ConsumerError(f"{where}: env must name an environment variable")
        if env and environ.get(env):
            root, origin = environ[env], env
        if not isinstance(root, str) or not root:
            raise ConsumerError(f"{where}: root must be a path")
        families = entry.get("adapters", [])
        if not isinstance(families, list) or not all(isinstance(item, str) for item in families):
            raise ConsumerError(f"{where}: adapters must be a list of adapter families")
        bad = [item for item in families if item not in FAMILIES]
        if bad:
            raise ConsumerError(f"{where}: unknown adapter famil{'y' if len(bad) == 1 else 'ies'} "
                                f"{', '.join(bad)} (known: {', '.join(FAMILIES)})")
        path = pathlib.Path(expand_vars(root, environ)).expanduser()
        if not path.is_absolute() and base is not None:
            path = base / path
        result.append(Consumer(name, path.resolve(), tuple(families), origin))
    return result


def expand_vars(text: str, environ) -> str:
    """Expand `$VAR` and `${VAR}` from `environ`; unknown variables stay as written."""
    return re.sub(r"\$(\w+|\{[^}]*\})", lambda match: environ.get(match.group(1).strip("{}"), match.group(0)), text)


def load_consumers(studio_root: pathlib.Path, environ=None) -> list:
    """The studio consumer followed by every configured consumer repository.

    Reads `design-system/consumers.toml` (or `$WHITE_PILL_CONSUMERS`); without
    one, the dotfiles checkout at `$WHITE_PILL_DOTFILES` or `~/.dotfiles`
    receives every adapter family.
    """
    studio_root = pathlib.Path(studio_root).resolve()
    path = consumers_path(studio_root, environ)
    if path.is_file():
        try:
            with path.open("rb") as file:
                document = tomllib.load(file)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConsumerError(f"Unable to read {path}: {error}") from error
        configured = parse_consumers(document, environ, path.parent.parent)
    else:
        configured = [
            Consumer(item.name, item.root, item.families, "default" if item.origin == "config" else item.origin)
            for item in parse_consumers({"consumer": DEFAULT_CONSUMERS}, environ)
        ]
    return [Consumer(STUDIO, studio_root, ("studio-css",), "studio"), *configured]


def git_state(root: pathlib.Path) -> dict | None:
    """`{"head": short sha or None, "dirty": bool}` for a git work tree, else None."""
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=5, check=False)

    try:
        if git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            return None
        head = git("rev-parse", "--short", "HEAD")
        status = git("status", "--porcelain")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {"head": head.stdout.strip() if head.returncode == 0 else None, "dirty": bool(status.stdout.strip())}


# ── Targets ──────────────────────────────────────────────────────────────


@dataclass
class Target:
    path: pathlib.Path
    family: str  # an adapter family (FAMILIES), or "studio-css" for the studio consumer
    mode: str | None
    kind: str  # toml | lua | qml | css
    consumer: str  # consumer repository name
    root: pathlib.Path  # consumer repository root
    blocks: list = field(default_factory=list)

    @property
    def display(self) -> str:
        return f"{self.consumer}:{self.path.relative_to(self.root)}"


def family_targets(family: str, consumer: Consumer) -> list:
    """Targets of one adapter family inside a consumer repository."""
    base = consumer.root

    def target(path, mode, kind, blocks=None):
        return Target(base / path, family, mode, kind, consumer.name, base, blocks or [])

    if family == "studio-css":
        return [target("design-system/tokens.css", None, "css")]
    if family == "omarchy-plugin":
        return [target(f"omarchy/plugins/{plugin}/DesignTokens.js", None, "qml") for plugin in QML_PLUGINS]
    if family == "omarchy-theme":
        return [target(f"omarchy-themes/{slug}/{name}", mode, "toml", blocks)
                for mode, slug in OMARCHY_THEMES.items() for name, blocks in OMARCHY_THEME.items()]
    if family == "hyprland":
        return [target(f"omarchy-themes/{slug}/{HYPRLAND_WINDOW_FILE}", mode, "lua", HYPRLAND_WINDOW)
                for mode, slug in OMARCHY_THEMES.items()]
    if family == "hunk":
        return [target(f"hunk/config.{mode}.toml", mode, "toml", HUNK) for mode in OMARCHY_THEMES]
    return []


# Order of families inside one consumer; it keeps generated output order stable.
FAMILY_ORDER = ("studio-css", "omarchy-plugin", "omarchy-theme", "hyprland", "hunk")


def targets(consumers: list) -> list:
    """Every generated target, consumer by consumer.

    Within a consumer, mode-independent targets (studio CSS, plugins) come
    first, then per mode the Omarchy theme files, Hyprland window chrome, and
    Hunk config (the historical order).
    """
    result = []
    for consumer in consumers:
        found = [target for family in FAMILY_ORDER if family in consumer.families
                 for target in family_targets(family, consumer)]
        result += [target for target in found if target.mode is None]
        for mode in OMARCHY_THEMES:
            result += [target for target in found if target.mode == mode]
    return result


def display_path(path: pathlib.Path, consumers: list) -> str:
    """`<consumer>:<relpath>` for the consumer with the most specific root containing `path`."""
    matches = [consumer for consumer in consumers if path.is_relative_to(consumer.root)]
    if not matches:
        return str(path)
    consumer = max(matches, key=lambda item: len(item.root.parts))
    return f"{consumer.name}:{path.relative_to(consumer.root)}"


# ── Values ───────────────────────────────────────────────────────────────


def css_name(name: str) -> str:
    return name.replace("_", "-")


def compact(value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def toml_scalar(value, style: str | None = None) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        if style == "compact":
            return compact(value)
        if style == "0.00":
            return f"{value:.2f}"
        return str(value)
    raise TokenError(f"Unsupported value {value!r}")


def hypr_rgba(color, alpha) -> str:
    """`#RRGGBB` and an alpha from 0 to 1 as Hyprland's `rgba(rrggbbaa)`."""
    if not isinstance(color, str) or not HEX_COLOR.fullmatch(color):
        raise TokenError(f"Not a #RRGGBB color: {color!r}")
    if not is_number(alpha) or not 0 <= alpha <= 1:
        raise TokenError(f"Not an alpha from 0 to 1: {alpha!r}")
    return f"rgba({color[1:].lower()}{round(alpha * 255):02x})"


def resolve_source(resolver: Resolver, source, mode: str | None) -> dict:
    """Resolve one adapter source to its value and provenance."""
    if isinstance(source, RGBA):
        color = resolve_source(resolver, source.color, mode)
        alpha = resolve_source(resolver, source.alpha, mode)
        return {**color, "chain": [*color["chain"], *alpha["chain"]],
                "value": hypr_rgba(color["value"], alpha["value"])}
    if isinstance(source, Literal):
        return {"recipe": None, "recipeField": None, "token": None, "chain": [], "value": source.for_mode(mode)}
    if is_reference(source):
        path = resolver.target(source, mode, "adapter")
        resolution = resolver.resolve(path, mode)
        return {
            "recipe": None, "recipeField": None, "token": resolution.origin,
            "chain": [resolution.origin, *resolution.chain], "value": resolution.value,
        }
    recipe, _, recipe_field = source.partition(".")
    resolution = resolver.resolve(f"recipe.{recipe}.{recipe_field}", mode)
    chain = [resolution.origin, *resolution.chain]
    token = next((path for path in chain if not is_recipe_path(path)), None)
    return {
        "recipe": recipe, "recipeField": recipe_field, "token": token,
        "chain": chain, "value": resolution.value,
    }


def unresolved_source(source) -> dict:
    """Best-effort provenance for a source that does not resolve (manifest only)."""
    primary = source.color if isinstance(source, RGBA) else source
    primary = primary if isinstance(primary, str) else ""
    token = primary[1:] if is_reference(primary) else None
    recipe, _, recipe_field = ("", "", "") if token is not None else primary.partition(".")
    return {"recipe": recipe or None, "recipeField": recipe_field or None, "token": token, "chain": [], "value": None}


def expand_rows(resolver: Resolver, block_rows, mode: str | None) -> list:
    if isinstance(block_rows, Passthrough):
        table = block_rows.table.replace("{mode}", mode or "")
        try:
            node = node_at(resolver.raw, table)
        except KeyError:
            return []
        return [(key, f"@{table}.{key}") for key, value in node.items()
                if key not in block_rows.exclude and not isinstance(value, dict)]
    return block_rows


def toml_blocks(resolver: Resolver, target: Target, strict: bool) -> list:
    """`(section, fields)` per block of a TOML or Lua target; `strict` raises on unresolved sources."""
    blocks = []
    for section, block_rows in target.blocks:
        fields = []
        for key, source, *style in expand_rows(resolver, block_rows, target.mode):
            try:
                resolved = resolve_source(resolver, source, target.mode)
            except ReferenceProblem as error:
                if strict:
                    raise TokenError(f"{target.path}: {section or '(top)'}.{key}: {error}") from None
                resolved = unresolved_source(source)
            fields.append({"section": section, "key": key, **resolved, "style": style[0] if style else None})
        blocks.append((section, fields))
    return blocks


def toml_fields(resolver: Resolver, target: Target, strict: bool) -> list:
    return [item for _, fields in toml_blocks(resolver, target, strict) for item in fields]


def css_properties(resolver: Resolver, strict: bool) -> list:
    raw = resolver.raw
    properties = []

    def add(section, name, path, suffix_for=None):
        try:
            value = resolver.value(path)
        except ReferenceProblem as error:
            if strict:
                raise TokenError(f"tokens.css: {error}") from None
            properties.append({"section": section, "key": f"--{name}", "token": path, "value": None})
            return
        suffix = suffix_for(value) if suffix_for else ""
        properties.append({"section": section, "key": f"--{name}", "token": path, "value": f"{compact(value)}{suffix}"})

    def table(path):
        node = raw
        for part in path.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        return node if isinstance(node, dict) else {}

    def px_if_number(value):
        return "px" if is_number(value) else ""

    for key in table("wide-gamut"):
        add(":root", f"wide-gamut-{css_name(key)}", f"wide-gamut.{key}")
    for section in ("type", "spacing", "radius", "blur", "motion"):
        for key in table(section):
            if section in {"spacing", "radius", "blur"} or (section == "type" and key.startswith("size-")):
                suffix_for = px_if_number
            elif section == "motion" and key in {"fast", "standard", "slow"}:
                suffix_for = lambda value: "ms"
            else:
                suffix_for = None
            add(":root", f"{section}-{css_name(key)}", f"{section}.{key}", suffix_for)
    for family in ("subtle", "glass", "glass-dark"):
        for key in table(f"border.{family}"):
            add(":root", f"border-{family}-{css_name(key)}", f"border.{family}.{key}",
                (lambda value: "px") if key.endswith("width") else None)
    for family in ("low", "floating"):
        for key in table(f"shadow.{family}"):
            add(":root", f"shadow-{family}-{css_name(key)}", f"shadow.{family}.{key}",
                (lambda value: "px") if key in {"blur", "x", "y"} else None)
    for mode in MODES:
        for group in ("color", "control", "chrome"):
            for key in table(f"{group}.{mode}"):
                add(mode, f"{group}-{css_name(key)}", f"{group}.{mode}.{key}")
    if "accent" in table("wide-gamut"):
        properties += [{"section": "p3", "key": f"--{name}", "token": "wide-gamut.accent",
                        "value": "var(--wide-gamut-accent)"} for name in P3_OVERRIDES]
    return properties


def css_document(resolver: Resolver) -> str:
    properties = css_properties(resolver, strict=True)

    def block(section, indent="  "):
        return [f"{indent}{item['key']}: {item['value']};" for item in properties if item["section"] == section]

    lines = ["/* Generated by script/design-system. Do not edit. */", ":root {", *block(":root"), "}"]
    for mode in MODES:
        lines.append(f'\n:root[data-theme="{mode}"], .theme-{mode} {{')
        lines.extend(block(mode))
        lines.append("}")
    p3_accent = resolver.value("wide-gamut.accent")
    lines.extend([
        "",
        f"@supports (color: {p3_accent}) {{",
        "  @media (color-gamut: p3) {",
        '    :root[data-theme="light"], .theme-light,',
        '    :root[data-theme="dark"], .theme-dark {',
        *block("p3", "      "),
        "    }",
        "  }",
        "}",
    ])
    return "\n".join(lines) + "\n"


QML_ADAPTER = '''// Generated by script/design-system. Do not edit.
.pragma library

function raw(values, key, fallback) {
  if (!values) return fallback
  var value = values[key]
  return value === undefined || value === null || String(value).length === 0 ? fallback : value
}

function number(values, key, fallback) {
  var value = Number(raw(values, key, fallback))
  return isFinite(value) ? value : fallback
}

function color(values, key, fallback) {
  return String(raw(values, key, fallback))
}

function shadow(values, section) {
  var prefix = String(section || "popups") + "."
  return {
    color: color(values, prefix + "shadow-color", "#10131A"),
    alpha: number(values, prefix + "shadow-alpha", 0.12),
    blur: number(values, prefix + "shadow-blur", 18),
    x: number(values, prefix + "shadow-x", 0),
    y: number(values, prefix + "shadow-y", 4),
  }
}

function innerBorder(values, section) {
  var prefix = String(section || "popups") + "."
  return {
    color: color(values, prefix + "inner-border", "#2C363C"),
    alpha: number(values, prefix + "inner-border-alpha", 0.14),
    width: number(values, prefix + "inner-border-width", 0.5),
  }
}

function gutterLeft(shadow) { return Math.max(0, shadow.blur - shadow.x) }
function gutterTop(shadow) { return Math.max(0, shadow.blur - shadow.y) }
function gutterRight(shadow) { return Math.max(0, shadow.blur + shadow.x) }
function gutterBottom(shadow) { return Math.max(0, shadow.blur + shadow.y) }
'''


# ── Entry points ─────────────────────────────────────────────────────────


def outputs(raw: dict, consumers: list) -> dict:
    """Generated file contents keyed by absolute path for `consumers` (see
    `load_consumers`). Raises TokenError."""
    resolver = Resolver(raw)
    result = {}
    for target in targets(consumers):
        if target.kind == "css":
            result[target.path] = css_document(resolver)
        elif target.kind == "qml":
            result[target.path] = QML_ADAPTER
        elif target.kind == "lua":
            result[target.path] = render_lua_target(resolver, target)
        else:
            result[target.path] = render_toml_target(resolver, target)
    return result


def lua_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return compact(value)
    raise TokenError(f"Unsupported value {value!r}")


def render_lua_target(resolver: Resolver, target: Target) -> str:
    """A generated Lua module that returns one nested config table."""
    tree: dict = {}
    for item in toml_fields(resolver, target, strict=True):
        node = tree
        for part in (item["section"] or "").split("."):
            if part:
                node = node.setdefault(part, {})
        node[item["key"]] = item["value"]

    def render(node: dict, depth: int) -> list:
        lines, indent = [], "\t" * depth
        for key, value in node.items():
            if not isinstance(value, dict):
                lines.append(f"{indent}{key} = {lua_scalar(value)},")
            elif key in LUA_POSITIONAL:
                lines.append(f"{indent}{key} = {{ {', '.join(lua_scalar(item) for item in value.values())} }},")
            elif key in LUA_INLINE:
                inner = ", ".join(f"{name} = {lua_scalar(item)}" for name, item in value.items())
                lines.append(f"{indent}{key} = {{ {inner} }},")
            else:
                lines += [f"{indent}{key} = {{", *render(value, depth + 1), f"{indent}}},"]
        return lines

    name = OMARCHY_THEME_NAMES.get(target.mode, target.mode)
    header = [
        "-- Generated by script/design-system. Do not edit.",
        f"-- {name} window chrome from White Pill Studio's recipe.window. The theme's",
        f'-- hyprland.lua applies it: hl.config(require("{HYPRLAND_WINDOW_MODULE}"))',
    ]
    return "\n".join([*header, "return {", *render(tree, 1), "}"]) + "\n"


def render_toml_target(resolver: Resolver, target: Target) -> str:
    blocks = []
    for section, fields in toml_blocks(resolver, target, strict=True):
        lines = [f"[{section}]"] if section else []
        lines += [f"{item['key']} = {toml_scalar(item['value'], item['style'])}" for item in fields]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def target_fields(resolver: Resolver, target: Target) -> list:
    """Manifest fields of one target (tolerates unresolvable sources)."""
    if target.kind == "css":
        return [
            {"section": item["section"], "key": item["key"], "recipe": None, "recipeField": None,
             "token": item["token"], "chain": [item["token"]], "value": item["value"]}
            for item in css_properties(resolver, strict=False)
        ]
    if target.kind == "qml":
        return []
    return [
        {key: value for key, value in item.items() if key != "style"}
        for item in toml_fields(resolver, target, strict=False)
    ]


def manifest(raw: dict, root: pathlib.Path, consumers: list) -> dict:
    """Machine-readable description of every generated target and field.

    Each target names its consumer repository (`consumer`, `root`), its
    adapter `family`, and a `display` label of the form `<consumer>:<relpath>`.
    `root` (the studio root) is unused: every path comes from `consumers`.
    It stays in the signature for callers such as captures.py.
    """
    resolver = Resolver(raw)
    result = []
    for target in targets(consumers):
        result.append({
            "path": str(target.path),
            "display": target.display,
            "consumer": target.consumer,
            "root": str(target.root),
            "family": target.family,
            "mode": target.mode,
            "fields": target_fields(resolver, target),
        })
    return {
        "consumers": [consumer.to_json() for consumer in consumers],
        "targets": result,
    }


def provenance(raw: dict, manifest_data: dict) -> dict:
    """Per-token provenance: foundation → semantic role → recipe → consumer field.

    Every scalar path in the raw document gets an entry. For recipe fields,
    which resolve per mode, `resolvesFrom` is the raw template target (it may
    contain `{mode}`), `chain` is empty, and `modes` holds each mode's chain
    and value. `usedBy` lists every generated TOML field whose resolution
    passes through the token, grouped by the recipe field that reaches it
    (`recipe`/`field` are null for direct token mappings).
    """
    resolver = Resolver(raw)
    result = {}

    for path, value in resolver.flat.items():
        entry = {"resolvesFrom": None, "chain": [], "referencedBy": [], "usedBy": []}
        if is_recipe_path(path):
            if is_reference(value):
                entry["resolvesFrom"] = value[1:]
            entry["modes"] = {}
            for mode, lookup in resolver.recipe_lookups(path):
                try:
                    resolution = resolver.resolve(lookup, mode)
                    entry["modes"][mode] = {"chain": resolution.chain, "value": resolution.value}
                except ReferenceProblem:
                    entry["modes"][mode] = {"chain": [], "value": None}
        elif is_reference(value):
            try:
                resolution = resolver.resolve(path)
                entry["resolvesFrom"] = resolution.chain[0]
                entry["chain"] = resolution.chain
            except ReferenceProblem:
                entry["resolvesFrom"] = value[1:]
        result[path] = entry

    for path, value in resolver.flat.items():
        if not is_reference(value):
            continue
        modes = [mode for mode, _ in resolver.recipe_lookups(path)] if is_recipe_path(path) else [None]
        for mode in modes:
            try:
                target = resolver.target(value, mode, path)
                canonical, _ = resolver.lookup(target, mode)
            except ReferenceProblem:
                continue
            if canonical in result and path not in result[canonical]["referencedBy"]:
                result[canonical]["referencedBy"].append(path)

    for target in manifest_data["targets"]:
        if target["family"] == "studio-css":
            continue
        for item in target["fields"]:
            consumer = {"display": target["display"], "section": item["section"], "key": item["key"]}
            for path in item["chain"]:
                if path not in result:
                    continue
                used = result[path]["usedBy"]
                group = next((entry for entry in used
                              if entry["recipe"] == item["recipe"] and entry["field"] == item["recipeField"]), None)
                if group is None:
                    group = {"recipe": item["recipe"], "field": item["recipeField"], "consumers": []}
                    used.append(group)
                group["consumers"].append(consumer)
    return result
