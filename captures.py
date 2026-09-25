"""Native capture harness: deterministic Omarchy fixtures captured with grim.

The harness applies this repository's generated theme (colors.toml plus the
merged shell.toml) to the *running* shell with `omarchy shell shell applyTheme`,
shows one fixture at a time, captures it at the monitor's native scale, and
always restores the user's theme payload, cursor setting, and notification
history afterwards. It never changes the persistent Omarchy theme.

Every external command goes through an injectable runner so tests can drive
the whole flow without a desktop.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime
import hashlib
import json
import math
import os
import pathlib
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import unquote

import cdp

ROOT = pathlib.Path(__file__).resolve().parent
OMARCHY_PATH = pathlib.Path(os.environ.get("OMARCHY_PATH", "/usr/share/omarchy"))
TOKENS = ROOT / "design-system" / "tokens.toml"
DEFAULT_OUT = ROOT / "captures"
MODES = ("light", "dark")
THEME_DIRS = {"light": "mekanikos-light", "dark": "mekanikos"}
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(png|json)$")
FIXTURE_SUMMARY = "White Pill capture fixture"
FIXTURE_BODY = "Deterministic notification used for native reference captures."
FIXTURE_APP = "White Pill Fixture"
HISTORY_LIMIT = 10  # mirrors historyLimit in the notifications service
MARGIN = 24  # logical px around a detected card, enough for the floating shadow


class CaptureError(Exception):
    pass


# ---------------------------------------------------------------- fixture plan

@dataclasses.dataclass(frozen=True)
class Surface:
    name: str
    label: str
    status: str  # supported | needs-fixture | manual
    method: str
    specimen: str  # CSS selector of the live specimen inside a mode preview
    deterministic: bool = False
    namespace: str = ""
    section: str = ""  # shell.toml section whose background the surface paints
    reason: str = ""
    live: tuple[str, ...] = ()
    view: str = "shell"  # studio view whose light/dark previews hold the specimen
    selector: str = ""  # full selector template with {mode}; default "#<view>-view .{mode}-preview <specimen>"

    def specimen_selector(self, mode: str) -> str:
        if self.selector:
            return self.selector.format(mode=mode)
        return f"#{self.view}-view .{mode}-preview {self.specimen}"


PLAN = (
    Surface(
        "osd", "OSD", "supported",
        "omarchy osd -i volume-medium -p 50 -d 0 (persistent, display only); card located by "
        "background-probe diff inside the fullscreen omarchy-osd layer; closed with `omarchy shell osd close`",
        ".osd-specimen", deterministic=True, namespace="omarchy-osd", section="popups",
    ),
    Surface(
        "notification", "Notification", "supported",
        f"omarchy notification send --app-name '{FIXTURE_APP}' -t 30000 '{FIXTURE_SUMMARY}' …; card "
        "located by a background-probe diff inside the fullscreen omarchy-notifications layer; dismissed by "
        "summary, then its history entry is removed and trimmed entries are restored",
        ".notification-specimen", deterministic=True, namespace="omarchy-notifications",
        section="notifications",
    ),
    Surface(
        "bar", "Bar", "supported",
        "grim of the omarchy-bar layer box (best effort: content is the live session)",
        ".menubar", deterministic=False, namespace="omarchy-bar", view="windows",
        # The full-width bar of the 2560-logical-px desktop specimen, like the native layer box.
        selector="#windows-view .desktop-specimen.{mode}-preview .menubar",
        reason="Clock, workspaces, tray, and indicators show live session state. Freezing the clock "
        "requires writing ~/.config/omarchy/shell.json (a dotfiles symlink), which the harness does not do.",
        live=("clock", "workspaces", "tray", "indicators"),
    ),
    Surface(
        "windows", "Windows", "supported",
        "headless Hyprland output sized like the monitor; ghostty (captures/fixtures/terminal.sh) tiled left and "
        "the default browser (Helium, throwaway profile) tiled right on its workspace; `grim -o` of the whole output",
        ".desktop-specimen", deterministic=True, view="windows", live=("bar",),
        selector="#windows-view .desktop-specimen.{mode}-preview",
        reason="The fixture windows are fixed. The top strip is the live omarchy-bar the shell puts on the headless "
        "output, the wallpaper is the user's current background, and the terminal is drawn active with per-window "
        "props (Hyprland has no per-window shadow color, so its shadow uses color_inactive).",
    ),
    Surface(
        "terminal", "Terminal", "supported",
        "crop of the windows capture: the ghostty client box from `hyprctl clients -j`, grown by border_size",
        ".terminal-specimen", deterministic=True, view="windows",
        selector="#windows-view .desktop-specimen.{mode}-preview .hypr-window.is-terminal",
        reason="ghostty runs the user's real config chain (~/.config/ghostty/config, which includes "
        "~/.dotfiles/ghostty/config last) with only the theme include swapped for the mode's ghostty.conf; the "
        "effective values (font, size, padding, cell height, cursor color) are in the metadata.",
    ),
    Surface(
        "browser", "Browser", "supported",
        "crop of the windows capture: the Helium client box from `hyprctl clients -j`, grown by border_size",
        ".browser-specimen", deterministic=True, view="windows",
        selector="#windows-view .desktop-specimen.{mode}-preview .hypr-window.is-browser",
        reason="Chrome color is Chromium managed policy (machine-wide, the installed theme). The harness runs the "
        "browser in a private mount namespace that sees a copy of the policy directory with this mode's "
        "chromium.theme color and scheme, so the chrome follows the mode; the page follows it through ?scheme=.",
    ),
    Surface(
        "tooltip", "Tooltip", "needs-fixture", "", ".tooltip-specimen",
        reason="Bar tooltips open only on real pointer hover; Omarchy exposes no IPC to show one, and "
        "moving the user's pointer is not acceptable in a live session.",
    ),
    Surface(
        "audio-panel", "Audio panel", "needs-fixture", "", ".panel-specimen",
        reason="`omarchy shell omarchy.audio open` renders live PipeWire devices and volumes; a fixed "
        "state needs a mock PipeWire graph or a panel fixture mode upstream.",
    ),
    Surface(
        "hunk", "Hunk", "manual", "", ".hunk-window",
        reason="Hunk is not installed (the ~/.local/bin/hunk mise shim would change global mise "
        "config). Upstream has a headless PTY-to-PNG path (tuistory + ghostty-opentui) to adopt later.",
    ),
)
SURFACES = {surface.name: surface for surface in PLAN}


def supported() -> list[Surface]:
    return [surface for surface in PLAN if surface.status == "supported"]


# ------------------------------------------------------------ command running

@dataclasses.dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[..., Result]


def run_command(command: list[str], *, timeout: float = 20, input: str | None = None) -> Result:
    try:
        completed = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout, input=input, check=False
        )
    except FileNotFoundError:
        return Result(127, "", f"{command[0]}: not found")
    except subprocess.TimeoutExpired:
        return Result(124, "", f"{' '.join(command)}: timed out")
    return Result(completed.returncode, completed.stdout, completed.stderr)


def output_of(run: Runner, command: list[str], default: str = "") -> str:
    result = run(command)
    return result.stdout.strip() if result.returncode == 0 else default


def checked(run: Runner, command: list[str], **kwargs) -> str:
    result = run(command, **kwargs)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise CaptureError(f"{' '.join(command[:4])} failed: {detail or result.returncode}")
    return result.stdout


# --------------------------------------------------------- shell.toml merging

HEADER_RE = re.compile(r"^\s*\[[^]]+\]\s*($|#)")
MIX_RE = re.compile(
    r"\{\{\s*(mix|mix_strip|mix_rgb)\s+([A-Za-z0-9_]+)\s+([A-Za-z0-9_]+)\s+([0-9]+(?:\.[0-9]+)?%?)\s*\}\}"
)
GRADIENT_RE = re.compile(r"\{\{\s*(shell_gradient|gradient_start)\s+([^}]+?)\s*\}\}")
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
ANGLE_RE = re.compile(r"^-?[0-9]+(\.[0-9]+)?deg$")


def parse_color_table(text: str) -> dict[str, str]:
    """Parse `omarchy-theme-color --all` output (key<TAB>value lines)."""
    colors = {}
    for line in text.splitlines():
        if "\t" in line:
            key, value = line.split("\t", 1)
            colors[key] = value
    return colors


def hex_rgb(value: str) -> str:
    return ",".join(str(int(value[index:index + 2], 16)) for index in (1, 3, 5))


def mix_color(start: str, end: str, amount: str) -> str:
    if amount.endswith("%"):
        fraction = float(amount[:-1]) / 100
    else:
        fraction = float(amount)
        if fraction > 1:
            fraction /= 100
    fraction = min(1.0, max(0.0, fraction))
    channels = []
    for index in (1, 3, 5):
        a, b = int(start[index:index + 2], 16), int(end[index:index + 2], 16)
        channels.append(int(a * (1 - fraction) + b * fraction + 0.5))
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def _theme_ref(colors: dict, ref: str, fallback: str) -> str:
    if ref in colors:
        return colors[ref]
    if fallback and fallback in colors:
        return colors[fallback]
    return fallback or ref


def _gradient(colors: dict, spec: str) -> tuple[list[str], str]:
    stops, angle = [], ""
    for part in spec.split():
        if ANGLE_RE.match(part):
            angle = part[:-3]
        else:
            stops.append(colors.get(part.strip(), part.strip()))
    return stops, angle


def _shell_hex(color: str) -> str:
    if re.match(r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$", color):
        return "#" + color[1:7]
    return color


def render_template(template: str, colors: dict[str, str]) -> str:
    """Mirror omarchy-theme-set-templates' substitutions for shell.toml.tpl."""

    def mix(match: re.Match) -> str:
        function, start_key, end_key, amount = match.groups()
        start, end = colors.get(start_key, ""), colors.get(end_key, "")
        if not (HEX_RE.match(start) and HEX_RE.match(end)):
            return match.group(0)
        value = mix_color(start, end, amount)
        if function == "mix_strip":
            return value[1:]
        if function == "mix_rgb":
            return hex_rgb(value)
        return value

    def gradient(match: re.Match) -> str:
        function, arguments = match.groups()
        parts = arguments.split()
        ref, fallback = parts[0], parts[1] if len(parts) > 1 else ""
        spec = _theme_ref(colors, ref, fallback)
        stops, angle = _gradient(colors, spec)
        if function == "gradient_start":
            return _shell_hex(stops[0] if stops else spec)
        if not stops:
            return spec
        return " ".join(stops) + (f" {angle}deg" if angle else "")

    text = MIX_RE.sub(mix, template)
    text = GRADIENT_RE.sub(gradient, text)
    for key, value in colors.items():
        text = text.replace(f"{{{{ {key} }}}}", value)
        text = text.replace(f"{{{{ {key}_strip }}}}", value.removeprefix("#"))
        if HEX_RE.match(value):
            text = text.replace(f"{{{{ {key}_rgb }}}}", hex_rgb(value))
    return text


def overlay_section(shell: str, section: str, override: str) -> str:
    """Replace (or append) one [section] exactly like the awk in omarchy-theme-set-templates."""
    own_header = re.compile(r"^\s*\[" + re.escape(section) + r"\]\s*($|#)")
    body = [line for line in override.splitlines() if not (HEADER_RE.match(line) and own_header.match(line))]
    lines = shell.splitlines()
    output: list[str] = []
    emitted = in_section = False

    def emit():
        nonlocal emitted
        if not emitted:
            output.append(f"[{section}]")
            output.extend(body)
            emitted = True

    for line in lines:
        if HEADER_RE.match(line):
            if in_section:
                emit()
                in_section = False
            if own_header.match(line):
                in_section = True
                continue
        if not in_section:
            output.append(line)
    if in_section or not emitted:
        if lines:
            output.append("")
        emit()
    return "\n".join(output) + "\n"


def merge_shell_toml(base: str, fragments: dict[str, str]) -> str:
    """Overlay shell.<section>.toml fragments (sorted by file name, like the bash glob)."""
    merged = base
    for name in sorted(fragments):
        match = re.fullmatch(r"shell\.([A-Za-z0-9_-]+)\.toml", name)
        if match:
            merged = overlay_section(merged, match.group(1), fragments[name])
    return merged


# ------------------------------------------------------------- theme payloads

@dataclasses.dataclass
class Payload:
    colors: str
    shell: str
    source: str

    def digest(self) -> dict:
        return {
            "colors_sha256": hashlib.sha256(self.colors.encode()).hexdigest(),
            "shell_sha256": hashlib.sha256(self.shell.encode()).hexdigest(),
        }


def probe_payload(payload: Payload, section: str, color: str) -> Payload:
    """Same payload with one section's background forced opaque to `color`.

    Captured with the fixture on screen, a frame under this payload differs from
    the real frame only where that surface paints its background, whatever live
    windows sit behind it. That diff locates the card precisely.
    """
    output, current = [], ""
    for line in payload.shell.splitlines():
        header = re.match(r"^\s*\[([^]]+)\]", line)
        if header:
            current = header.group(1)
        elif current == section:
            key = line.split("=", 1)[0].strip() if "=" in line else ""
            if key == "background":
                line = f'background = "{color}"'
            elif key == "background-alpha":
                line = "background-alpha = 1.0"
        output.append(line)
    return Payload(payload.colors, "\n".join(output) + "\n", payload.source + f" (probe {section})")


def dotfiles_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("WHITE_PILL_DOTFILES", "~/.dotfiles")).expanduser().resolve()


def theme_dir(mode: str, dotfiles: pathlib.Path) -> pathlib.Path:
    """The directory the generator writes this mode's colors.toml into.

    Reads the adapter manifest for the configured consumers (family
    `omarchy-theme`); falls back to the known dotfiles layout when the
    manifest cannot be built.
    """
    try:
        import adapters  # noqa: PLC0415
        import tokens_model  # noqa: PLC0415

        manifest = adapters.manifest(tokens_model.load(TOKENS), ROOT, adapters.load_consumers(ROOT))
        for target in manifest["targets"]:
            family = target.get("family") or target.get("consumer")
            if family == "omarchy-theme" and target.get("mode") == mode and target["path"].endswith("/colors.toml"):
                return pathlib.Path(target["path"]).parent
    except Exception as error:  # the manifest is optional here; fall back to the known layout
        print(f"capture: adapter manifest unavailable ({error}); using {dotfiles}", file=sys.stderr)
    return dotfiles / "omarchy-themes" / THEME_DIRS[mode]


def themed_file(directory: pathlib.Path, name: str, run: Runner, omarchy_path: pathlib.Path,
                home: pathlib.Path) -> str:
    """A theme's `name` as omarchy-theme-set-templates writes it.

    The theme's own file wins; otherwise `name`.tpl (the user's, else Omarchy's) is rendered with
    the theme's colors resolved by `omarchy-theme-color --all`.
    """
    own = directory / name
    if own.is_file():
        return own.read_text()
    user = home / ".config" / "omarchy" / "themed" / f"{name}.tpl"
    template = user if user.is_file() else omarchy_path / "default" / "themed" / f"{name}.tpl"
    table = parse_color_table(checked(run, ["omarchy-theme-color", "--file", str(directory / "colors.toml"), "--all"]))
    return render_template(template.read_text(), table)


def build_payload(directory: pathlib.Path, run: Runner, omarchy_path: pathlib.Path, home: pathlib.Path) -> Payload:
    colors_path = directory / "colors.toml"
    if not colors_path.is_file():
        raise CaptureError(f"No generated colors.toml at {colors_path}")
    base = themed_file(directory, "shell.toml", run, omarchy_path, home)
    fragments = {path.name: path.read_text() for path in directory.glob("shell.*.toml")}
    return Payload(colors_path.read_text(), merge_shell_toml(base, fragments), str(directory))


def current_payload(state: pathlib.Path) -> Payload:
    """What the running shell loaded from the user's current theme."""
    directory = state / "current" / "theme"
    try:
        return Payload((directory / "colors.toml").read_text(), (directory / "shell.toml").read_text(), str(directory))
    except OSError as error:
        raise CaptureError(f"Cannot read the current theme payload: {error}") from error


# ------------------------------------------------------------------ pixel math

def read_ppm(path: pathlib.Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    tokens, index = [], 0
    while len(tokens) < 4:
        while data[index:index + 1].isspace():
            index += 1
        if data[index:index + 1] == b"#":
            index = data.index(b"\n", index) + 1
            continue
        start = index
        while not data[index:index + 1].isspace():
            index += 1
        tokens.append(data[start:index])
    if tokens[0] != b"P6" or int(tokens[3]) != 255:
        raise CaptureError(f"Unsupported PPM {path}")
    width, height = int(tokens[1]), int(tokens[2])
    return width, height, data[index + 1:index + 1 + width * height * 3]


def png_size(path: pathlib.Path) -> tuple[int, int]:
    with path.open("rb") as file:
        header = file.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise CaptureError(f"{path} is not a PNG")
    return struct.unpack(">II", header[16:24])


def _longest_run(flags: list[bool], gap: int = 3) -> tuple[int, int] | None:
    best, start, last = None, None, None
    for index, flag in enumerate(flags):
        if not flag:
            continue
        if start is None or index - last > gap:
            start = index
        last = index
        if best is None or last - start > best[1] - best[0]:
            best = (start, last)
    return best


def changed_box(before: bytes, after: bytes, width: int, height: int, threshold: int = 6,
                share: float = 0.2) -> tuple[int, int, int, int] | None:
    """Pixel box (x, y, w, h) of the dominant changed block; small live noise is ignored."""
    stride = width * 3
    row_counts = [0] * height
    column_counts = [0] * width
    changed_rows = {}
    for y in range(height):
        a, b = before[y * stride:(y + 1) * stride], after[y * stride:(y + 1) * stride]
        if a == b:
            continue
        columns = [x for x in range(width)
                   if max(abs(a[3 * x] - b[3 * x]), abs(a[3 * x + 1] - b[3 * x + 1]),
                          abs(a[3 * x + 2] - b[3 * x + 2])) > threshold]
        row_counts[y] = len(columns)
        changed_rows[y] = columns
    peak = max(row_counts, default=0)
    if peak == 0:
        return None
    rows = _longest_run([count >= max(3, peak * share) for count in row_counts])
    for y in range(rows[0], rows[1] + 1):
        for x in changed_rows.get(y, ()):
            column_counts[x] += 1
    column_peak = max(column_counts)
    columns = _longest_run([count >= max(3, column_peak * share) for count in column_counts])
    return columns[0], rows[0], columns[1] - columns[0] + 1, rows[1] - rows[0] + 1


def logical_box(pixel_box, region, scale: float, margin: int) -> dict:
    """Convert a pixel box inside `region` to a logical box grown by `margin`, clamped to `region`."""
    px, py, pw, ph = pixel_box
    left = max(region["x"], region["x"] + math.floor(px / scale) - margin)
    top = max(region["y"], region["y"] + math.floor(py / scale) - margin)
    right = min(region["x"] + region["width"], region["x"] + math.ceil((px + pw) / scale) + margin)
    bottom = min(region["y"] + region["height"], region["y"] + math.ceil((py + ph) / scale) + margin)
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


def geometry(box: dict) -> str:
    return f"{box['x']},{box['y']} {box['width']}x{box['height']}"


# --------------------------------------------------------------------- desktop

class Desktop:
    """Thin wrapper over the omarchy, hyprctl, and grim commands the harness uses."""

    def __init__(self, run: Runner = run_command, sleep: Callable[[float], None] = time.sleep,
                 home: pathlib.Path | None = None):
        self.run = run
        self.sleep = sleep
        self.home = home or pathlib.Path.home()
        self.state = self.home / ".local" / "state" / "omarchy"

    def json(self, command: list[str]):
        try:
            return json.loads(checked(self.run, command))
        except json.JSONDecodeError as error:
            raise CaptureError(f"{' '.join(command)} returned invalid JSON") from error

    def poll(self, check: Callable[[], object], timeout: float, interval: float = 0.1):
        """Call `check` until it returns something truthy, sleeping `interval` up to `timeout`; its last value."""
        for _ in range(max(1, round(timeout / interval))):
            value = check()
            if value:
                return value
            self.sleep(interval)
        return check()

    def eval(self, code: str) -> None:
        result = self.run(["hyprctl", "eval", code])
        text = (result.stdout + result.stderr).strip()
        if result.returncode or text.lower().startswith("error") or "error:" in text.lower():
            raise CaptureError(f"hyprctl eval failed: {text or result.returncode}")

    def dispatch(self, expression: str) -> None:
        result = self.run(["hyprctl", "dispatch", expression])
        text = (result.stdout + result.stderr).strip()
        if result.returncode or "error" in text.lower():
            raise CaptureError(f"hyprctl dispatch failed: {text or result.returncode}")

    def option(self, name: str) -> dict:
        return self.json(["hyprctl", "getoption", name, "-j"])

    def monitor(self) -> dict:
        monitors = self.json(["hyprctl", "monitors", "-j"])
        if not monitors:
            raise CaptureError("hyprctl reports no monitors")
        chosen = next((item for item in monitors if item.get("focused")), monitors[0])
        scale = float(chosen["scale"])
        transform = int(chosen.get("transform", 0))
        width, height = chosen["width"], chosen["height"]
        if transform % 2:
            width, height = height, width
        return {
            "name": chosen["name"], "scale": scale, "x": chosen["x"], "y": chosen["y"],
            "width": round(width / scale), "height": round(height / scale),
        }

    def layer(self, monitor: str, namespace: str) -> dict | None:
        layers = self.json(["hyprctl", "layers", "-j"]).get(monitor, {}).get("levels", {})
        for level in layers.values():
            for item in level:
                if item.get("namespace") == namespace:
                    return {"x": item["x"], "y": item["y"], "width": item["w"], "height": item["h"]}
        return None

    def wait_layer(self, monitor: str, namespace: str, present: bool, timeout: float = 3.0) -> dict | None:
        self.poll(lambda: (self.layer(monitor, namespace) is not None) == present, timeout)
        return self.layer(monitor, namespace)

    def cursor_setting(self) -> int:
        return int(self.option("cursor:no_hardware_cursors")["int"])

    def set_cursor_setting(self, value: int) -> None:
        try:
            self.eval(f"hl.config({{ cursor = {{ no_hardware_cursors = {int(value)} }} }})")
        except CaptureError:
            checked(self.run, ["hyprctl", "keyword", "cursor:no_hardware_cursors", str(int(value))])

    def apply_theme(self, payload: Payload) -> None:
        colors = base64.b64encode(payload.colors.encode()).decode()
        shell = base64.b64encode(payload.shell.encode()).decode()
        out = checked(self.run, ["omarchy", "shell", "shell", "applyTheme", colors, shell])
        if "ok" not in out:
            raise CaptureError(f"applyTheme did not confirm: {out.strip()!r}")

    def grim(self, box: dict, path: pathlib.Path, scale: float, kind: str = "png") -> None:
        checked(self.run, ["grim", "-g", geometry(box), "-s", f"{scale:g}", "-t", kind, str(path)])

    def is_dnd(self) -> bool:
        return output_of(self.run, ["omarchy", "shell", "notifications", "isDnd"]).lower() in {"on", "true", "1"}


# ---------------------------------------------------------------- fixtures

class Fixture:
    """A shell card on `monitor_name`: region(monitor, bar) to search, show(), and close() -> problems.

    close() undoes show() and is safe to call more than once, or without show().
    """

    def __init__(self, desktop: Desktop, monitor_name: str):
        self.desktop = desktop
        self.monitor_name = monitor_name
        self.shown = False


class OsdFixture(Fixture):
    def region(self, monitor, bar):
        width, height = min(1000, monitor["width"]), min(360, monitor["height"])
        return {"x": monitor["x"] + (monitor["width"] - width) // 2,
                "y": monitor["y"] + monitor["height"] - height, "width": width, "height": height}

    def show(self):
        if self.desktop.layer(self.monitor_name, "omarchy-osd"):
            raise CaptureError("An OSD is already on screen; try again in a moment")
        self.shown = True
        checked(self.desktop.run, ["omarchy", "osd", "-i", "volume-medium", "-p", "50", "-d", "0"])

    def close(self):
        if not self.shown:
            return []
        result = self.desktop.run(["omarchy", "shell", "osd", "close"])
        self.shown = False
        self.desktop.wait_layer(self.monitor_name, "omarchy-osd", present=False)
        return [] if result.returncode == 0 else [f"osd close failed: {result.stderr.strip()}"]


class NotificationFixture(Fixture):
    def region(self, monitor, bar):
        top = monitor["y"] + (bar["height"] if bar else 0) + 1
        width = min(640, monitor["width"])
        return {"x": monitor["x"] + monitor["width"] - width, "y": top,
                "width": width, "height": min(360, monitor["height"] - top)}

    @property
    def history(self) -> pathlib.Path:
        return self.desktop.state / "notifications" / "history"

    @property
    def images(self) -> pathlib.Path:
        return self.desktop.state / "notifications" / "images"

    def show(self):
        if self.desktop.is_dnd():
            raise CaptureError("Do Not Disturb is on; the notification would go straight to history")
        if self.desktop.layer(self.monitor_name, "omarchy-notifications"):
            raise CaptureError("Another notification is on screen; try again when it is gone")
        self.backup = pathlib.Path(tempfile.mkdtemp(prefix="white-pill-history-"))
        self.before = set()
        for source, name in ((self.history, "history"), (self.images, "images")):
            target = self.backup / name
            target.mkdir()
            if source.is_dir():
                for item in source.iterdir():
                    if item.is_file():
                        shutil.copy2(item, target / item.name)
                        if name == "history":
                            self.before.add(item.name)
        self.shown = True
        checked(self.desktop.run, ["omarchy", "notification", "send", "--app-name", FIXTURE_APP, "-t", "30000",
                                   FIXTURE_SUMMARY, FIXTURE_BODY])

    def pairs(self):
        return ((self.backup / "history", self.history), (self.backup / "images", self.images))

    def missing_from_backup(self) -> list[str]:
        return [item.name for source, target in self.pairs() for item in source.iterdir()
                if not (target / item.name).exists()]

    def restore_backup(self) -> list[str]:
        restored = []
        for source, target in self.pairs():
            for item in source.iterdir():
                if not (target / item.name).exists():
                    target.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, target / item.name)
                    restored.append(item.name)
        return restored

    def fixture_entries(self) -> list[pathlib.Path]:
        found = []
        if not self.history.is_dir():
            return found
        for item in self.history.glob("*.json"):
            if item.name in self.before:
                continue
            try:
                entry = json.loads(item.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if entry.get("summary") == FIXTURE_SUMMARY and entry.get("app") == FIXTURE_APP:
                found.append(item)
        return found

    def close(self):
        if not self.shown:
            return []
        problems = []
        sleep = self.desktop.sleep
        entries: list[pathlib.Path] = []
        for _attempt in range(3):
            self.desktop.run(["omarchy", "shell", "notifications", "dismiss", FIXTURE_SUMMARY])
            entries = self.desktop.poll(self.fixture_entries, 3.0)
            if entries:
                break
        if not entries:
            problems.append("fixture notification did not reach history; check "
                            f"{self.history} for '{FIXTURE_SUMMARY}'")
        # The shell archives (mv) and then trims history to the newest HISTORY_LIMIT in
        # one job; wait for that trim so a restored entry is not deleted after us.
        self.desktop.poll(lambda: len(list(self.history.glob("*.json"))) <= HISTORY_LIMIT, 3.0)
        sleep(0.3)
        for entry in entries:
            entry.unlink(missing_ok=True)
            for image in self.images.glob(f"{entry.stem}-*"):
                image.unlink(missing_ok=True)
        missing = []
        for _attempt in range(3):
            self.restore_backup()
            sleep(0.4)
            missing = self.missing_from_backup()
            if not missing:
                break
        if missing:
            problems.append(f"history entries still missing after restore ({', '.join(missing)}); "
                            f"backup kept at {self.backup}")
        else:
            shutil.rmtree(self.backup, ignore_errors=True)
        self.shown = False
        self.desktop.wait_layer(self.monitor_name, "omarchy-notifications", present=False)
        return problems


FIXTURES = {"osd": OsdFixture, "notification": NotificationFixture}


# ------------------------------------------------------ window chrome: theme

WINDOW_SURFACES = ("windows", "terminal", "browser")
HEADLESS_OUTPUT = "WHITEPILL-CAPTURE"
FIXTURES_DIR = ROOT / "captures" / "fixtures"
TERMINAL_SCRIPT = FIXTURES_DIR / "terminal.sh"
BROWSER_PLACEHOLDER = FIXTURES_DIR / "browser-placeholder.html"
BROWSER_PAGE_PATH = "/fixtures/browser-page.html"
DEFAULT_STUDIO_URL = "http://127.0.0.1:4777"
TERMINAL_TRANSCRIPT = ROOT / "web" / "fixtures" / "terminal-session.ans"  # shared with the studio specimen
BROWSER_WINDOW_SIZE = (1264, 1387)
BROWSER_POLICY = pathlib.Path("/etc/chromium/policies/managed/color.json")
BROWSER_POLICY_DEFAULT_COLOR = "#1c2027"  # install/helpers/browser-policy.sh
# Omarchy's own opacity rules (default/hypr/windows.lua and apps/browser.lua), in force when the
# theme's `o.window(".*", …)` rule sets no opacity of its own. (active, inactive)
OMARCHY_OPACITY = {"terminal": ("0.985", "0.96"), "browser": ("1.0", "0.985")}
# The terminal runs the user's real ghostty config chain (~/.config/ghostty/config and everything it
# includes) with only the Omarchy theme include swapped for the captured mode's ghostty.conf.
# These flags only make it a separate, disposable process; none of them changes how it looks.
GHOSTTY_FIXTURE_FLAGS = (
    "--gtk-single-instance=false",
    "--quit-after-last-window-closed=true",
    "--linux-cgroup=never",
    "--shell-integration=none",
    "--app-notifications=false",
    "--title=White Pill terminal fixture",
)
THEME_INCLUDE_RE = re.compile(r"^[ \t]*config-file[ \t]*=[ \t]*\??[ \t]*\"?[^\n\"]*current/theme/ghostty\.conf\"?[ \t]*$", re.M)
# Effective ghostty keys recorded in metadata (from `ghostty +show-config`).
GHOSTTY_KEYS = (
    "font-family", "font-style", "font-size", "adjust-cell-height", "adjust-cell-width", "window-padding-x",
    "window-padding-y", "window-padding-balance", "window-colorspace", "window-theme", "theme", "background",
    "foreground", "cursor-style", "cursor-style-blink", "cursor-color", "cursor-text", "selection-background",
    "selection-foreground", "palette", "gtk-toolbar-style", "window-decoration",
)
# Options recorded in metadata (and compared before/after when a mode needs a change).
CHROME_OPTIONS = (
    "general:border_size", "general:gaps_in", "general:gaps_out", "general:col.active_border",
    "general:col.inactive_border", "general:layout", "decoration:rounding", "decoration:rounding_power",
    "decoration:border_part_of_window", "decoration:dim_inactive", "decoration:shadow:enabled",
    "decoration:shadow:range", "decoration:shadow:render_power", "decoration:shadow:sharp",
    "decoration:shadow:color", "decoration:shadow:color_inactive", "decoration:shadow:offset",
    "decoration:shadow:scale",
)
WINDOW_PROPS = ("opacity", "opacity_inactive", "active_border_color", "inactive_border_color", "rounding")

LUA_TOKEN = re.compile(r"""
    (?P<space>\s+)
  | (?P<comment>--\[\[.*?\]\]|--[^\n]*)
  | (?P<string>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<punct>.)
""", re.S | re.X)


class LuaSkip(Exception):
    pass


def lua_tokens(text: str) -> list[tuple[str, str]]:
    return [(match.lastgroup, match.group()) for match in LUA_TOKEN.finditer(text)
            if match.lastgroup not in ("space", "comment")]


def _lua_string(token: str) -> str:
    return re.sub(r"\\(.)", lambda match: {"n": "\n", "t": "\t"}.get(match.group(1), match.group(1)), token[1:-1])


def _lua_value(tokens, index: int, names: dict):
    kind, text = tokens[index]
    if kind == "string":
        return _lua_string(text), index + 1
    if kind == "number":
        return (float(text) if "." in text else int(text)), index + 1
    if text == "-" and index + 1 < len(tokens) and tokens[index + 1][0] == "number":
        value, index = _lua_value(tokens, index + 1, names)
        return -value, index
    if kind == "name":
        if text in ("true", "false"):
            return text == "true", index + 1
        if text == "nil":
            return None, index + 1
        if index + 1 < len(tokens) and tokens[index + 1][1] in ("(", ".", ":", "["):
            raise LuaSkip(text)
        if text not in names:
            raise LuaSkip(text)
        return names[text], index + 1
    if text == "{":
        return _lua_table(tokens, index + 1, names)
    raise LuaSkip(text)


def _lua_table(tokens, index: int, names: dict):
    keyed, positional = {}, []
    while tokens[index][1] != "}":
        kind, text = tokens[index]
        if kind == "name" and tokens[index + 1][1] == "=":
            keyed[text], index = _lua_value(tokens, index + 2, names)
        elif text == "[":
            key, index = _lua_value(tokens, index + 1, names)
            if tokens[index][1] != "]" or tokens[index + 1][1] != "=":
                raise LuaSkip("[")
            keyed[key], index = _lua_value(tokens, index + 2, names)
        else:
            value, index = _lua_value(tokens, index, names)
            positional.append(value)
        if tokens[index][1] in (",", ";"):
            index += 1
    if keyed and positional:
        keyed.update({position + 1: value for position, value in enumerate(positional)})
    return (keyed if keyed or not positional else positional), index + 1


def deep_merge(target: dict, source: dict) -> dict:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict) and not is_leaf(value):
            deep_merge(target[key], value)
        else:
            target[key] = value
    return target


def theme_lua_text(directory: pathlib.Path) -> str:
    """A theme's hyprland.lua, preceded by the generated window fragment it requires first."""
    fragment = pathlib.Path(directory) / "hyprland_window.lua"
    prefix = fragment.read_text() + "\n" if fragment.is_file() else ""
    return prefix + (pathlib.Path(directory) / "hyprland.lua").read_text()


def parse_theme_lua(text: str) -> dict:
    """The literal parts of a theme's hyprland.lua: merged hl.config tables and o.window rules.

    Handles string/number/boolean `local` constants and table constructors; statements with
    other expressions are skipped.
    """
    tokens = lua_tokens(text) + [("eof", "")]
    names: dict = {}
    config: dict = {}
    windows: list = []
    index = 0
    while index < len(tokens) - 1:
        texts = [token[1] for token in tokens[index:index + 4]]
        try:
            if texts[0] == "local" and tokens[index + 1][0] == "name" and texts[2] == "=":
                names[texts[1]], index = _lua_value(tokens, index + 3, names)
                continue
            if texts[:2] == ["return", "{"]:  # a generated fragment's returned config table
                table, index = _lua_value(tokens, index + 1, names)
                if isinstance(table, dict):
                    deep_merge(config, table)
                continue
            if texts == ["hl", ".", "config", "("]:
                table, index = _lua_value(tokens, index + 4, names)
                if isinstance(table, dict):
                    deep_merge(config, table)
                continue
            if texts == ["o", ".", "window", "("]:
                match, index = _lua_value(tokens, index + 4, names)
                if tokens[index][1] != ",":
                    raise LuaSkip(",")
                rules, index = _lua_value(tokens, index + 1, names)
                windows.append({"match": match, "rules": rules})
                continue
        except (LuaSkip, IndexError):
            pass
        index += 1
    return {"config": config, "windows": windows}


def is_leaf(value) -> bool:
    """Config leaves: scalars, vec2 lists, gradient tables, and CSS gap tables."""
    if not isinstance(value, dict):
        return True
    return "colors" in value or (bool(value) and set(value) <= {"top", "right", "bottom", "left"})


def flatten_config(config: dict, path: tuple = ()) -> dict[tuple, object]:
    flat = {}
    for key, value in config.items():
        if not path and key == "plugin":  # plugin options are not queryable through getoption
            continue
        if is_leaf(value):
            flat[path + (key,)] = value
        else:
            flat.update(flatten_config(value, path + (key,)))
    return flat


def option_name(path: tuple) -> str:
    """('general', 'col', 'active_border') -> 'general:col.active_border' (hyprctl getoption name)."""
    parts: list[str] = []
    for segment in path:
        if parts and parts[-1] == "col":
            parts[-1] = f"col.{segment}"
        else:
            parts.append(str(segment))
    return parts[0] + ":" + ":".join(parts[1:])


def nest(values: dict[tuple, object]) -> dict:
    tree: dict = {}
    for path, value in values.items():
        node = tree
        for segment in path[:-1]:
            node = node.setdefault(segment, {})
        node[path[-1]] = value
    return tree


def theme_color(token: str) -> str | None:
    """Config color -> 'aarrggbb' (Hyprland's getoption order), or None."""
    token = token.strip()
    match = re.fullmatch(r"rgba?\(\s*([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?\s*\)", token)
    if match:
        return ((match.group(2) or "ff") + match.group(1)).lower()
    match = re.fullmatch(r"0x([0-9A-Fa-f]{8})", token)
    return match.group(1).lower() if match else None


def _gradient_key(colors: list[str], angle: float):
    if len(colors) == 1 and not angle:
        return colors[0]
    return ("gradient", tuple(colors), round(float(angle), 3))


def canonical(value):
    """A comparable form for a theme value (Lua side)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    if isinstance(value, str):
        parts = value.split()
        colors = [theme_color(part) for part in parts if not ANGLE_RE.match(part)]
        if parts and all(colors):
            angles = [float(part[:-3]) for part in parts if ANGLE_RE.match(part)]
            return _gradient_key(colors, angles[0] if angles else 0)
        return value.strip().lower()
    if isinstance(value, list):
        return tuple(canonical(item) for item in value)
    if isinstance(value, dict) and "colors" in value:
        return _gradient_key([theme_color(color) or str(color) for color in value["colors"]], value.get("angle", 0))
    if isinstance(value, dict):
        sides = tuple(round(float(value.get(side, 0)), 4) for side in ("top", "right", "bottom", "left"))
        return sides[0] if len(set(sides)) == 1 else ("css", sides)
    return value


def live_value(option: dict):
    """`hyprctl getoption -j` output -> the equivalent Lua config value (as Python data)."""
    if "gradient" in option:
        parts = option["gradient"].split()
        colors = [part.zfill(8).lower() for part in parts if not ANGLE_RE.match(part)]
        angles = [float(part[:-3]) for part in parts if ANGLE_RE.match(part)]
        rgba = [f"rgba({color[2:]}{color[:2]})" for color in colors]
        angle = angles[0] if angles else 0
        if len(rgba) == 1 and not angle:
            return rgba[0]
        return {"colors": rgba, "angle": int(angle) if float(angle).is_integer() else angle}
    if "css" in option:
        sides = [int(float(part)) for part in option["css"].split()]
        if len(set(sides)) == 1:
            return sides[0]
        return dict(zip(("top", "right", "bottom", "left"), sides))
    if "vec2" in option:
        return [int(v) if float(v).is_integer() else v for v in option["vec2"]]
    for key in ("int", "float", "bool", "str"):
        if key in option:
            return option[key]
    raise CaptureError(f"Unknown getoption value for {option.get('option')}: {option}")


def gradient_prop(value) -> str:
    """A live color value (string or gradient table) as a set_prop / legacy gradient string."""
    if isinstance(value, dict):
        return " ".join(value["colors"]) + (f" {value['angle']}deg" if value.get("angle") else "")
    return str(value)


def lua_literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "{ " + ", ".join(lua_literal(item) for item in value) + " }"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{key} = {lua_literal(item)}" for key, item in value.items()) + " }"
    raise CaptureError(f"Cannot express {value!r} in Lua")


def chrome_delta(mode_theme: dict, installed_theme: dict, live: dict[str, dict]) -> tuple[dict, list]:
    """Options the mode's theme sets differently from the installed theme.

    Returns ({path: mode value} to apply, [skipped notes]). An option whose live value does not
    match the installed theme is overridden later in the user's config (e.g. looknfeel.lua), and
    would be in the other mode too, so it is left alone.
    """
    mode_flat = flatten_config(mode_theme["config"])
    installed_flat = flatten_config(installed_theme["config"])
    delta, skipped = {}, []
    for path, value in sorted(mode_flat.items(), key=lambda item: option_name(item[0])):
        if path in installed_flat and canonical(installed_flat[path]) == canonical(value):
            continue
        name = option_name(path)
        current = live.get(name)
        if current is not None and path in installed_flat and \
                canonical(live_value(current)) != canonical(installed_flat[path]):
            skipped.append(f"{name}: user config overrides the theme value")
            continue
        delta[path] = value
    return delta, skipped


def catch_all_opacity(theme: dict) -> tuple[str, str] | None:
    """(active, inactive) opacity from the theme's `o.window(".*", …)` rules, if any."""
    found = None
    for rule in theme["windows"]:
        if rule["match"] == ".*" and isinstance(rule["rules"], dict) and "opacity" in rule["rules"]:
            parts = str(rule["rules"]["opacity"]).split()
            found = (parts[0], parts[1] if len(parts) > 1 else parts[0])
    return found


def window_opacity(role: str, theme: dict) -> tuple[str, str]:
    """Effective (active, inactive) opacity for a fixture window under `theme` (theme rules load last)."""
    return catch_all_opacity(theme) or OMARCHY_OPACITY[role]


def ghostty_user_config(config_dir: pathlib.Path, theme_conf: pathlib.Path) -> str:
    """The user's ghostty config with the Omarchy theme include pointed at `theme_conf`.

    Relative config-file paths are made absolute (ghostty resolves them against the including
    file's directory, which moves when the config is copied).
    """
    path = config_dir / "config"
    if not path.is_file():
        path = config_dir / "config.ghostty"
    text = path.read_text() if path.is_file() else ""
    text, swapped = THEME_INCLUDE_RE.subn(f'config-file = "{theme_conf}"', text)
    if not swapped:
        text += f'\nconfig-file = "{theme_conf}"\n'

    def absolute(match: re.Match) -> str:
        optional, value = match.group(1), match.group(2)
        if value.startswith(("/", "~")):
            return match.group(0)
        return f'config-file = {optional}"{config_dir / value}"'

    return re.sub(r'^[ \t]*config-file[ \t]*=[ \t]*(\??)[ \t]*"?([^\n"]+?)"?[ \t]*$', absolute, text, flags=re.M)


def parse_ghostty_config(text: str) -> dict:
    """`ghostty +show-config` output -> {key: value or [values] for repeated keys}."""
    found: dict = {}
    for line in text.splitlines():
        if " = " not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split(" = ", 1)
        key = key.strip()
        if key in found:
            found[key] = (found[key] if isinstance(found[key], list) else [found[key]]) + [value]
        else:
            found[key] = value
    return found


def ghostty_effective(config_text: str, work: pathlib.Path, run: Runner) -> dict:
    """Effective ghostty settings for `config_text` (`+show-config` reads only $XDG_CONFIG_HOME/ghostty)."""
    xdg = work / "ghostty-xdg"
    (xdg / "ghostty").mkdir(parents=True, exist_ok=True)
    (xdg / "ghostty" / "config").write_text(config_text)
    result = run(["env", f"XDG_CONFIG_HOME={xdg}", "ghostty", "+show-config"])
    shown = parse_ghostty_config(result.stdout) if result.returncode == 0 else {}
    if isinstance(shown.get("palette"), list):  # the 16 ANSI colors; 16-255 are ghostty's fixed cube
        shown["palette"] = [entry for entry in shown["palette"] if int(entry.split("=", 1)[0]) < 16]
    return {key: shown[key] for key in GHOSTTY_KEYS if key in shown}


# ------------------------------------------------------- window chrome: geometry

def client_box(client: dict, border: int) -> dict:
    """Global logical box of a client including its border (clients -j excludes the border)."""
    (x, y), (width, height) = client["at"], client["size"]
    return {"x": x - border, "y": y - border, "width": width + 2 * border, "height": height + 2 * border}


def relative_box(box: dict, output: dict) -> dict:
    return {**box, "x": box["x"] - output["x"], "y": box["y"] - output["y"]}


def pixel_box(box: dict, scale: float) -> dict:
    """Logical box -> the device-pixel box grim writes for it (grim truncates each value)."""
    return {key: int(box[key] * scale) for key in ("x", "y", "width", "height")}


def split_windows(clients: list[dict]) -> dict[str, dict]:
    """Pick the terminal and browser fixture clients; the terminal must be tiled left of the browser."""
    terminal = next((c for c in clients if c.get("class") == "com.mitchellh.ghostty"), None)
    browser = next((c for c in clients if c.get("class") == "helium"), None)
    if terminal is None or browser is None:
        found = ", ".join(sorted(str(c.get("class", "?")) for c in clients)) or "none"
        raise CaptureError(f"Fixture windows did not both map (found: {found})")
    if terminal["at"][0] >= browser["at"][0]:
        raise CaptureError("The terminal fixture did not tile left of the browser")
    return {"terminal": terminal, "browser": browser}


# ------------------------------------------------------- window chrome: session

def process_tree(pid: int) -> list[int]:
    """pid and its descendants (Linux /proc)."""
    found, queue = [], [pid]
    while queue:
        current = queue.pop()
        found.append(current)
        for children in pathlib.Path(f"/proc/{current}/task").glob("*/children"):
            try:
                queue.extend(int(child) for child in children.read_text().split())
            except (OSError, ValueError):
                pass
    return found


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    status = pathlib.Path(f"/proc/{pid}/stat")
    try:
        return status.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def kill(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def processes_using(marker: str) -> list[int]:
    """PIDs whose command line mentions `marker` (a unique temp path), excluding this process."""
    found = []
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if marker.encode() in (entry / "cmdline").read_bytes():
                found.append(int(entry.name))
        except OSError:
            pass
    return found


def page_available(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


class PlaceholderServer:
    """Serves captures/fixtures/browser-placeholder.html while the studio page does not exist."""

    def __init__(self, page: pathlib.Path = BROWSER_PLACEHOLDER):
        body = page.read_bytes()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}{BROWSER_PAGE_PATH}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def wait_for_page(port: int, url: str, timeout: float = 20, sleep: Callable[[float], None] = time.sleep) -> str:
    """Wait (over DevTools) until the fixture page has loaded and its fonts are ready; returns the browser version."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1) as response:
                targets = json.load(response)
            page = next(t for t in targets if t.get("type") == "page" and t.get("url", "").startswith(url))
            with cdp.Browser(page["webSocketDebuggerUrl"], timeout=5) as tab:  # attach only; the harness owns the process
                tab.wait("document.readyState === 'complete' && document.fonts.status === 'loaded'",
                         "the fixture page", max(0.0, deadline - time.monotonic()))
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
                return json.load(response).get("Browser", "")
        except (OSError, StopIteration, ValueError, KeyError, cdp.CDPError):
            if time.monotonic() > deadline:
                raise CaptureError(f"The browser fixture never loaded {url}") from None
            sleep(0.2)


class WindowsFixture:
    """Two tiled fixture windows on a throwaway headless output.

    open() creates the output, applies the mode's window-chrome delta, and maps the terminal
    (left, drawn active) and browser (right, inactive). close() undoes all of it and returns the
    restore steps performed plus any problems; it is safe to call more than once.
    """

    def __init__(self, desktop: Desktop, mode: str, work: pathlib.Path, mode_theme_dir: pathlib.Path,
                 installed_theme_dir: pathlib.Path, page_url: str | None = None):
        self.desktop = desktop
        self.mode = mode
        self.work = work / f"windows-{mode}"
        self.mode_theme_dir = mode_theme_dir
        self.installed_theme_dir = installed_theme_dir
        self.page_url = page_url or DEFAULT_STUDIO_URL + BROWSER_PAGE_PATH
        self.output: dict | None = None
        self.created_output = False
        self.pids: dict[str, int] = {}
        self.addresses: set[str] = set()
        self.profile: pathlib.Path | None = None
        self.browser_command: str | None = None
        self.cursor_timeout: float | None = None  # set while the pointer is hidden
        self.option_snapshot: dict[tuple, dict] = {}  # option path -> getoption value before the chrome delta
        self.focus: dict = {}
        self.placeholder: PlaceholderServer | None = None
        self.restore_steps: list[str] = []
        self.info: dict = {}

    # -- helpers
    def option_or_none(self, name: str) -> dict | None:
        try:
            value = self.desktop.option(name)
            live_value(value)
            return value
        except CaptureError:
            return None

    def output_state(self) -> dict | None:
        for monitor in self.desktop.json(["hyprctl", "monitors", "-j"]):
            if monitor.get("name") == HEADLESS_OUTPUT:
                return monitor
        return None

    def workspace_clients(self) -> list[dict]:
        if not self.output:
            return []
        workspace = self.output["activeWorkspace"]["id"]
        return [client for client in self.desktop.json(["hyprctl", "clients", "-j"])
                if client.get("workspace", {}).get("id") == workspace]

    def wait_client(self, window_class: str, timeout: float = 15) -> dict:
        client = self.desktop.poll(lambda: next(
            (client for client in self.workspace_clients()
             if client.get("class") == window_class and client.get("mapped", True)), None), timeout, 0.2)
        if client is None:
            raise CaptureError(f"The {window_class} fixture window never mapped on {HEADLESS_OUTPUT}")
        return client

    def launch(self, name: str, command: list[str]) -> None:
        script = self.work / f"launch-{name}.sh"
        script.write_text("#!/bin/sh\nexec " + shlex.join(command) + "\n")
        script.chmod(0o755)
        workspace = self.output["activeWorkspace"]["id"]
        self.desktop.dispatch(f"hl.dsp.exec_cmd({json.dumps(str(script))}, {{ workspace = \"{workspace} silent\" }})")

    def set_prop(self, client: dict, prop: str, value: str) -> None:
        self.desktop.dispatch(f"hl.dsp.window.set_prop({{ window = \"address:{client['address']}\", "
                      f"prop = {json.dumps(prop)}, value = {json.dumps(value)} }})")

    def hide_cursor(self) -> None:
        """Hide the pointer while grim runs.

        Hyprland software-renders the pointer onto the headless output at its monitor-local
        position, so it would land in the capture. `cursor:invisible` only applies on the next
        pointer move; a short `cursor:inactive_timeout` hides it without touching the pointer.
        """
        self.cursor_timeout = live_value(self.desktop.option("cursor:inactive_timeout"))
        self.desktop.eval("hl.config({ cursor = { inactive_timeout = 0.1 } })")
        self.desktop.sleep(0.6)

    def show_cursor(self) -> str | None:
        if self.cursor_timeout is None:
            return None
        previous, self.cursor_timeout = self.cursor_timeout, None
        self.desktop.eval(f"hl.config({{ cursor = {{ inactive_timeout = {lua_literal(previous)} }} }})")
        if abs(float(live_value(self.desktop.option("cursor:inactive_timeout"))) - float(previous)) > 1e-6:
            raise CaptureError("cursor:inactive_timeout was not restored")
        return (f"restored cursor:inactive_timeout={previous:g} after hiding the pointer during grim (verified)")

    def props(self, client: dict) -> dict:
        found = {}
        for prop in WINDOW_PROPS:
            found[prop] = output_of(self.desktop.run, ["hyprctl", "getprop", f"address:{client['address']}", prop]) or None
        return found

    # -- lifecycle
    def open(self) -> dict:
        self.work.mkdir(parents=True, exist_ok=True)
        focus = self.desktop.json(["hyprctl", "activewindow", "-j"]) or {}
        self.focus = {"address": focus.get("address"), "class": focus.get("class"),
                      "workspace": self.desktop.json(["hyprctl", "activeworkspace", "-j"]).get("id")}
        if self.output_state():
            raise CaptureError(f"{HEADLESS_OUTPUT} already exists (an earlier capture did not finish); "
                               f"remove it with `hyprctl output remove {HEADLESS_OUTPUT}`")

        # Window chrome for this mode: only the options the mode's theme sets differently.
        mode_theme = parse_theme_lua(theme_lua_text(self.mode_theme_dir))
        installed_theme = parse_theme_lua(theme_lua_text(self.installed_theme_dir))
        live = {}
        for path in flatten_config(mode_theme["config"]):
            value = self.option_or_none(option_name(path))
            if value is not None:
                live[option_name(path)] = value
        delta, skipped = chrome_delta(mode_theme, installed_theme, live)
        for path in [path for path in delta if option_name(path) not in live]:
            skipped.append(f"{option_name(path)}: not a queryable option, left alone")
            del delta[path]
        self.option_snapshot = {path: live[option_name(path)] for path in delta}
        self.info["theme_delta"] = {option_name(path): value for path, value in delta.items()}
        self.info["theme_delta_skipped"] = skipped
        if delta:
            self.desktop.eval(f"hl.config({lua_literal(nest(delta))})")
            self.restore_steps.append("applied " + ", ".join(self.info["theme_delta"]) + " from the mode's hyprland.lua")

        # ghostty: the user's config chain with this mode's generated ghostty.conf
        colors = self.work / "ghostty-theme.conf"
        colors.write_text(themed_file(self.mode_theme_dir, "ghostty.conf", self.desktop.run, OMARCHY_PATH,
                                      self.desktop.home))
        ghostty_conf = self.work / "ghostty.conf"
        ghostty_conf.write_text(ghostty_user_config(self.desktop.home / ".config" / "ghostty", colors))
        self.info["ghostty_theme_sha256"] = hashlib.sha256(colors.read_bytes()).hexdigest()
        self.info["ghostty_effective"] = ghostty_effective(ghostty_conf.read_text(), self.work, self.desktop.run)

        # Page: the studio's fixture page, else the local placeholder.
        if page_available(self.page_url):
            page, source = self.page_url, "studio"
        else:
            self.placeholder = PlaceholderServer()
            page, source = self.placeholder.url, "placeholder"
        page_with_mode = f"{page}?scheme={self.mode}"  # the fixture page forces its scheme from ?scheme=
        self.info["page"] = {"url": page_with_mode, "source": source,
                             **({"note": f"{self.page_url} was not available; served {BROWSER_PLACEHOLDER.name}"}
                                if source == "placeholder" else {})}

        # Headless output sized and scaled like the real monitor, far from it so the pointer cannot reach it.
        monitor = self.desktop.monitor()
        physical = f"{round(monitor['width'] * monitor['scale'])}x{round(monitor['height'] * monitor['scale'])}"
        self.desktop.eval(f"hl.monitor({{ output = \"{HEADLESS_OUTPUT}\", mode = \"{physical}@60\", "
                          f"position = \"{monitor['x'] + 20000}x{monitor['y']}\", scale = {monitor['scale']:g} }})")
        checked(self.desktop.run, ["hyprctl", "output", "create", "headless", HEADLESS_OUTPUT])
        self.created_output = True
        self.output = self.desktop.poll(self.output_state, 5.0)
        if not self.output:
            raise CaptureError(f"hyprctl never reported the {HEADLESS_OUTPUT} output")
        self.desktop.wait_layer(HEADLESS_OUTPUT, "omarchy-bar", present=True, timeout=3)

        # Terminal first so dwindle puts it on the left.
        self.launch("terminal", ["ghostty", "--config-default-files=false", f"--config-file={ghostty_conf}",
                                 *GHOSTTY_FIXTURE_FLAGS, "-e", str(TERMINAL_SCRIPT)])
        terminal = self.wait_client("com.mitchellh.ghostty")
        self.pids["terminal"] = terminal["pid"]
        self.profile = self.work / "browser-profile"
        self.profile.mkdir(exist_ok=True)
        debug_port = cdp.free_port()
        self.browser_command = browser_command(self.desktop.run)
        # Chrome colour: machine policy is the installed theme's, so run the browser alone in a mount
        # namespace that sees this mode's chromium.theme colour instead (the machine file is untouched).
        seed = chromium_theme_color(self.mode_theme_dir, self.desktop.run, OMARCHY_PATH, self.desktop.home)
        policy_dir = browser_policy_override(self.work, seed, self.mode, self.desktop.run)
        wrapper = []
        if policy_dir is not None:
            wrapper = policy_namespace(policy_dir)
            self.info["browser_policy_override"] = json.loads((policy_dir / BROWSER_POLICY.name).read_text())
        flags = ["--no-first-run", "--no-default-browser-check", "--password-store=basic", "--disable-sync",
                 "--hide-crash-restore-bubble", f"--window-size={BROWSER_WINDOW_SIZE[0]},{BROWSER_WINDOW_SIZE[1]}"]
        self.info["browser_flags"] = flags
        self.launch("browser", [*wrapper, self.browser_command, f"--user-data-dir={self.profile}",
                                f"--remote-debugging-port={debug_port}", *flags, page_with_mode])
        browser = self.wait_client("helium")
        self.pids["browser"] = browser["pid"]
        self.info["browser_version"] = wait_for_page(debug_port, page, sleep=self.desktop.sleep)

        # Per-window chrome: the mode's opacity, and the terminal drawn as the focused window.
        active_border = gradient_prop(live_value(self.desktop.option("general:col.active_border")))
        windows = split_windows(self.workspace_clients())
        self.addresses = {client["address"] for client in windows.values()}
        applied = {}
        for role, client in windows.items():
            active, inactive = window_opacity(role, mode_theme)
            if role == "terminal":
                inactive = active
                self.set_prop(client, "inactive_border_color", active_border)
            self.set_prop(client, "opacity", active)
            self.set_prop(client, "opacity_inactive", inactive)
            applied[role] = {"opacity": active, "opacity_inactive": inactive,
                             **({"inactive_border_color": active_border} if role == "terminal" else {})}
        self.info["window_props_set"] = applied
        self.desktop.sleep(1.2)  # open animations and the last paint
        return windows

    def close(self) -> tuple[list[str], list[str]]:
        steps, problems = list(self.restore_steps), []
        try:
            shown = self.show_cursor()
            if shown:
                steps.append(shown)
        except CaptureError as error:
            problems.append(f"restore cursor: {error}")
        stolen = False
        if self.focus.get("address"):
            try:
                now = self.desktop.json(["hyprctl", "activewindow", "-j"]) or {}
                stolen = now.get("address") in self.addresses or \
                    (self.output is not None and now.get("monitor") == self.output.get("id"))
            except CaptureError as error:
                problems.append(f"read focus: {error}")
        # 1. fixture windows and their processes
        for role, pid in self.pids.items():
            tree = process_tree(pid)
            kill(pid, signal.SIGTERM)
            self.desktop.poll(lambda: not any(pid_alive(item) for item in tree), 5.0)
            survivors = [item for item in tree if pid_alive(item)]
            for item in survivors:
                kill(item, signal.SIGKILL)
            steps.append(f"closed {role} fixture (pid {pid}, SIGTERM"
                         + (f", SIGKILL for {len(survivors)} survivors)" if survivors else ")"))
        self.pids = {}
        profile = self.profile
        if profile is not None:
            self.desktop.poll(lambda: not processes_using(str(profile)), 3.0)
            for item in processes_using(str(profile)):
                kill(item, signal.SIGKILL)
            if processes_using(str(profile)):
                problems.append(f"browser processes still use {profile}")
            shutil.rmtree(profile, ignore_errors=True)
            steps.append("removed the throwaway browser profile")
            self.profile = None
        if self.placeholder is not None:
            self.placeholder.close()
            self.placeholder = None
            steps.append("stopped the placeholder page server")
        # 2. the headless output
        if self.created_output:
            result = self.desktop.run(["hyprctl", "output", "remove", HEADLESS_OUTPUT])
            self.desktop.poll(lambda: not self.output_state(), 3.0)
            if self.output_state():
                problems.append(f"{HEADLESS_OUTPUT} still exists: {(result.stderr or result.stdout).strip()}")
            else:
                steps.append(f"removed headless output {HEADLESS_OUTPUT} (its in-memory monitor rule stays "
                             "until the next Hyprland reload and matches only that name)")
            self.created_output = False
        # 3. options
        if self.option_snapshot:
            names = [option_name(path) for path in self.option_snapshot]
            try:
                restore = {path: live_value(value) for path, value in self.option_snapshot.items()}
                self.desktop.eval(f"hl.config({lua_literal(nest(restore))})")
                changed = [option_name(path) for path, value in self.option_snapshot.items()
                           if canonical(live_value(self.desktop.option(option_name(path)))) != canonical(live_value(value))]
                if changed:
                    problems.append("options not restored: " + ", ".join(changed))
                else:
                    steps.append("restored " + ", ".join(names) + " (verified with getoption)")
            except CaptureError as error:
                problems.append(f"restore options: {error}")
            self.option_snapshot = {}
        # 4. focus: only if a fixture window took it
        if stolen:
            try:
                self.desktop.dispatch(f"hl.dsp.focus({{ window = \"address:{self.focus['address']}\" }})")
                steps.append(f"refocused {self.focus['class']} ({self.focus['address']}), which a fixture took")
            except CaptureError as error:
                problems.append(f"restore focus: {error}")
        elif self.focus.get("address"):
            steps.append("focus untouched (fixture windows opened silently)")
        self.focus = {}
        return steps, problems


def browser_command(run: Runner = run_command) -> str:
    """The default browser's launcher from its desktop file (Exec=), e.g. helium-browser."""
    desktop_id = output_of(run, ["xdg-settings", "get", "default-web-browser"]) or "helium.desktop"
    for base in (pathlib.Path.home() / ".local/share/applications", pathlib.Path("/usr/share/applications")):
        entry = base / desktop_id
        if entry.is_file():
            for line in entry.read_text().splitlines():
                if line.startswith("Exec="):
                    binary = line.removeprefix("Exec=").split()[0]
                    if shutil.which(binary):
                        return binary
    if shutil.which("helium-browser"):
        return "helium-browser"
    raise CaptureError(f"Cannot find the default browser ({desktop_id})")


def browser_policy(path: pathlib.Path = BROWSER_POLICY, override: dict | None = None) -> dict:
    """Chrome-colour policy metadata: the machine policy, and the per-capture override if one was used."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    if override:
        return {
            "policy": str(path),
            "theme_color": override["BrowserThemeColor"],
            "color_scheme": override["BrowserColorScheme"],
            "follows_mode": True,
            "method": "the browser runs in its own user+mount namespace (unshare, no fork, so Hyprland's "
                      "launch rule still matches its pid) where a per-capture copy of the managed policy "
                      f"directory is bind-mounted over {path.parent}; the machine policy is never written",
            "machine_policy": {"theme_color": data.get("BrowserThemeColor"),
                               "color_scheme": data.get("BrowserColorScheme")},
            "note": "BrowserThemeColor is this mode's chromium.theme (the template renders the palette "
                    "background, as omarchy-theme-set-browser would). Omarchy writes BrowserColorScheme "
                    "\"device\", and omarchy-theme-set-gnome sets the device scheme from the theme's mode, so the "
                    "override states that mode explicitly.",
        }
    return {
        "policy": str(path),
        "theme_color": data.get("BrowserThemeColor"),
        "color_scheme": data.get("BrowserColorScheme"),
        "follows_mode": False,
        "note": "Chromium-family chrome color is machine-wide managed policy written by omarchy-theme-set-browser "
                "for the installed theme, and the per-capture namespace override was unavailable, so the captured "
                "chrome keeps the installed theme's color in both modes.",
    }


def chromium_theme_color(directory: pathlib.Path, run: Runner, omarchy_path: pathlib.Path,
                         home: pathlib.Path) -> str:
    """BrowserThemeColor for a theme directory, as omarchy-theme-set-browser derives it.

    chromium.theme is the theme's own file, else the user's or Omarchy's chromium.theme.tpl
    (`{{ background_rgb }}`) rendered with the theme's colors; anything but three 0–255
    components falls back to Omarchy's default, as browser_policy_theme_hex does.
    """
    text = themed_file(directory, "chromium.theme", run, omarchy_path, home)
    match = re.fullmatch(r"\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*", text)
    if not match or any(int(part) > 255 for part in match.groups()):
        return BROWSER_POLICY_DEFAULT_COLOR
    return "#" + "".join(f"{int(part):02x}" for part in match.groups())


def policy_namespace(policy_dir: pathlib.Path, managed: pathlib.Path = BROWSER_POLICY.parent) -> list[str]:
    """Command prefix: a private user+mount namespace where `managed` shows `policy_dir`.

    `unshare` without --fork and `exec` keep the launched pid, which Hyprland needs to apply the
    launch's silent workspace rule to the window (bwrap forks, and its window would land on the
    user's workspace). setpriv drops the namespace capabilities before the browser starts.
    """
    return ["unshare", "--user", "--map-current-user", "--mount", "--keep-caps", "sh", "-c",
            f"mount --bind {shlex.quote(str(policy_dir))} {shlex.quote(str(managed))} && "
            'exec setpriv --inh-caps=-all --ambient-caps=-all "$0" "$@"']


def browser_policy_override(work: pathlib.Path, color: str, scheme: str, run: Runner = run_command,
                            source: pathlib.Path = BROWSER_POLICY) -> pathlib.Path | None:
    """A copy of the managed policy directory with this capture's chrome colour.

    None when there is no managed policy directory or the namespace cannot be made (no
    unprivileged user namespaces, or no unshare/setpriv); the capture then keeps the machine policy.
    """
    if not source.parent.is_dir() or not (shutil.which("unshare") and shutil.which("setpriv")):
        return None
    target = work / "browser-policy"
    target.mkdir(parents=True, exist_ok=True)
    for item in source.parent.iterdir():
        if item.is_file() and item.name != source.name:
            shutil.copyfile(item, target / item.name)
    (target / source.name).write_text(json.dumps({"BrowserThemeColor": color, "BrowserColorScheme": scheme}) + "\n")
    probe = run([*policy_namespace(target, source.parent), "cat", str(source)])
    if probe.returncode != 0 or color not in probe.stdout:
        return None
    return target


def window_metadata(fixture: WindowsFixture, windows: dict[str, dict], options: dict[str, dict],
                    props: dict[str, dict], run: Runner) -> dict:
    """The window-chrome part of the metadata, shared by windows/terminal/browser."""
    output = fixture.output or {}
    origin = {"x": output.get("x", 0), "y": output.get("y", 0)}
    border = int(live_value(options["general:border_size"])) if "general:border_size" in options else 0
    geometries = {}
    for role, client in windows.items():
        box = client_box(client, border)
        geometries[role] = {
            "class": client.get("class"), "at": client.get("at"), "size": client.get("size"),
            "box": relative_box(box, origin), "props": props.get(role, {}),
        }
    ghostty_version = output_of(run, ["ghostty", "--version"]).splitlines()
    effective = fixture.info.get("ghostty_effective", {})
    families = effective.get("font-family") or []
    families = families if isinstance(families, list) else [families]
    return {
        "output": {"name": output.get("name", HEADLESS_OUTPUT), "headless": True,
                   "size": [output.get("width"), output.get("height")], "scale": output.get("scale"),
                   "workspace": output.get("activeWorkspace", {}).get("id"),
                   "reserved": output.get("reserved")},
        "hyprland": {name: live_value(value) for name, value in options.items()},
        "theme_delta": fixture.info.get("theme_delta", {}),
        "theme_delta_skipped": fixture.info.get("theme_delta_skipped", []),
        "window_props_set": fixture.info.get("window_props_set", {}),
        "windows": geometries,
        "ghostty": {
            "version": ghostty_version[0] if ghostty_version else None,
            "config": "~/.config/ghostty/config chain with the theme include swapped for the mode's ghostty.conf",
            "flags": list(GHOSTTY_FIXTURE_FLAGS),
            "effective": effective,
            "theme_sha256": fixture.info.get("ghostty_theme_sha256"),
            "fonts": [font_match(run, family) for family in families],
            "script": str(TERMINAL_SCRIPT.relative_to(ROOT)),
            "transcript": str(TERMINAL_TRANSCRIPT.relative_to(ROOT)),
            "transcript_sha256": sha256_file(TERMINAL_TRANSCRIPT),
            "cursor": "hidden (the script prints DECTCEM hide) so no cursor cell differs between runs",
        },
        "browser": {
            "name": "Helium",
            "command": fixture.browser_command,
            "version": fixture.info.get("browser_version"),
            "package": output_of(run, ["pacman", "-Q", "helium-browser-bin"]) or None,
            "flags": fixture.info.get("browser_flags", []),
            "page": fixture.info.get("page"),
            "chrome": browser_policy(override=fixture.info.get("browser_policy_override")),
        },
    }


def capture_windows(desktop: Desktop, wanted: list[Surface], mode: str, out: pathlib.Path, work: pathlib.Path,
                    payload: Payload, session: dict, mode_theme_dir: pathlib.Path,
                    page_url: str | None = None) -> list[dict]:
    """One fixture session per mode yields every requested window surface."""
    fixture = WindowsFixture(desktop, mode, work, mode_theme_dir, desktop.state / "current" / "theme", page_url)
    shots: dict[str, tuple[dict, pathlib.Path]] = {}
    error: Exception | None = None
    try:
        windows = fixture.open()
        options = {name: desktop.option(name) for name in CHROME_OPTIONS}
        props = {role: fixture.props(client) for role, client in windows.items()}
        border = int(live_value(options["general:border_size"]))
        output = fixture.output = fixture.output_state() or fixture.output  # reserved area now includes the bar
        scale = float(output["scale"])
        fixture.hide_cursor()
        for surface in wanted:
            image = out / f"{surface.name}-{mode}.png"
            if surface.name == "windows":
                checked(desktop.run, ["grim", "-o", HEADLESS_OUTPUT, "-s", f"{scale:g}", str(image)])
                box = {"x": 0, "y": 0, "width": round(output["width"] / scale), "height": round(output["height"] / scale)}
            else:
                global_box = client_box(windows[surface.name], border)
                desktop.grim(global_box, image, scale)
                box = relative_box(global_box, output)
            shots[surface.name] = (box, image)
        shown = fixture.show_cursor()
        if shown:
            fixture.restore_steps.append(shown)
        chrome = window_metadata(fixture, windows, options, props, desktop.run)
    except (CaptureError, OSError) as caught:
        error = caught
    finally:
        steps, problems = fixture.close()
    if error is not None:
        detail = f"{error}" + (f"; restore problems: {'; '.join(problems)}" if problems else "")
        raise CaptureError(detail)
    monitor = {"name": HEADLESS_OUTPUT, "scale": scale}
    results = []
    for surface in wanted:
        box, image = shots[surface.name]
        data = metadata(surface, mode, box, monitor, image, payload, session,
                        "headless output" if surface.name == "windows"
                        else f"hyprctl clients -j ({chrome['windows'][surface.name]['class']}) grown by border_size",
                        extra={**chrome, "restore": steps})
        if surface.name != "windows":
            data["crop_of"] = f"windows-{mode}.png"
            data["crop_pixels"] = pixel_box(box, monitor["scale"])
        results.append(save_metadata(out, data, problems))
    if problems:
        raise CaptureError("; ".join(problems))
    return results


# ------------------------------------------------------------------- metadata

def sha256_file(path: pathlib.Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def package_versions(run: Runner, packages=("quickshell", "qt6-base", "hyprland", "grim")) -> dict:
    versions = {}
    for package in packages:
        parts = output_of(run, ["pacman", "-Q", package]).split()
        versions[package] = parts[1] if len(parts) == 2 else None
    return versions


def git_state(run: Runner, path: pathlib.Path) -> dict:
    commit = output_of(run, ["git", "-C", str(path), "rev-parse", "HEAD"])
    status = run(["git", "-C", str(path), "status", "--porcelain"])
    return {
        "path": str(path),
        "commit": commit or None,
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
    }


def studio_state(run: Runner, root: pathlib.Path = ROOT, tokens: pathlib.Path = TOKENS) -> dict:
    commit = output_of(run, ["git", "-C", str(root), "rev-parse", "HEAD"])
    return {"commit": commit or "uncommitted", "tokens_sha256": sha256_file(tokens)}


def font_match(run: Runner, family: str) -> dict:
    matched = output_of(run, ["fc-match", "--format=%{family[0]}|%{style[0]}|%{file}", family])
    name, style, file = (matched.split("|") + ["", "", ""])[:3] if matched else ("", "", "")
    return {
        "requested": family,
        "family": name or None,
        "style": style or None,
        "file": file or None,
        "fallback": bool(name) and not family.lower().startswith(name.lower()),
    }


def bar_family(tokens: pathlib.Path = TOKENS) -> str:
    try:
        with tokens.open("rb") as file:
            return tomllib.load(file)["type"]["bar"]
    except (OSError, KeyError, ValueError):
        return "Inter"


def environment(run: Runner, dotfiles: pathlib.Path) -> dict:
    """Session-wide metadata, gathered once per run."""
    return {
        "versions": package_versions(run),
        "dotfiles": git_state(run, dotfiles),
        "studio": studio_state(run),
        "user_theme": output_of(run, ["omarchy", "theme", "current"]) or None,
        "font": font_match(run, bar_family()),
    }


def metadata(surface: Surface, mode: str, box: dict, monitor: dict, image: pathlib.Path,
             payload: Payload, session: dict, located_by: str, now: datetime.datetime | None = None,
             backdrop: list[str] | None = None, extra: dict | None = None) -> dict:
    width, height = png_size(image)
    data = {
        "surface": surface.name,
        "mode": mode,
        "date": (now or datetime.datetime.now(datetime.timezone.utc)).isoformat(timespec="seconds"),
        "image": image.name,
        "box": box,
        "scale": monitor["scale"],
        "pixels": {"width": width, "height": height},
        "monitor": monitor["name"],
        "method": surface.method,
        "located_by": located_by,
        "deterministic": surface.deterministic,
        "live": list(surface.live),
        "notes": surface.reason,
        "backdrop": backdrop or [],
        "versions": session["versions"],
        "dotfiles": session["dotfiles"],
        "studio": session["studio"],
        "theme": {
            "user_theme": session["user_theme"],
            "applied": payload.source,
            **payload.digest(),
        },
        "font": session["font"],
    }
    data.update(extra or {})
    return data


def save_metadata(out: pathlib.Path, data: dict, problems: list[str]) -> dict:
    """Write `<surface>-<mode>.json`, recording any restore problems."""
    if problems:
        data["restore_problems"] = problems
    (out / f"{data['surface']}-{data['mode']}.json").write_text(json.dumps(data, indent=2) + "\n")
    return data


def backdrop_clients(desktop: Desktop, box: dict) -> list[str]:
    """Window classes under the box: translucent surfaces blur whatever is behind them."""
    try:
        clients = desktop.json(["hyprctl", "clients", "-j"])
        workspace = desktop.json(["hyprctl", "activeworkspace", "-j"]).get("id")
    except CaptureError:
        return []
    names = []
    for client in clients:
        if client.get("workspace", {}).get("id") != workspace or client.get("hidden"):
            continue
        (x, y), (w, h) = client.get("at", (0, 0)), client.get("size", (0, 0))
        if x < box["x"] + box["width"] and box["x"] < x + w and y < box["y"] + box["height"] and box["y"] < y + h:
            names.append(client.get("class") or client.get("initialClass") or "window")
    return sorted(set(names))


# -------------------------------------------------------------------- capture

def locate_card(desktop: Desktop, surface: Surface, mode: str, region: dict, scale: float, payload: Payload,
                work: pathlib.Path) -> dict:
    """Logical box of the card on screen: `region` grabbed as rendered and under a probe payload.

    The probe paints the surface's background opaque black (light) or white (dark), so only the
    card differs between the two frames, whatever sits behind it. The box is grown by MARGIN.
    """
    real, probe = work / f"{surface.name}-{mode}-real.ppm", work / f"{surface.name}-{mode}-probe.ppm"
    desktop.grim(region, real, scale, "ppm")
    try:
        desktop.apply_theme(probe_payload(payload, surface.section, "#000000" if mode == "light" else "#FFFFFF"))
        desktop.sleep(0.35)
        desktop.grim(region, probe, scale, "ppm")
    finally:
        desktop.apply_theme(payload)
    desktop.sleep(0.35)
    width, height, first = read_ppm(real)
    found = changed_box(first, read_ppm(probe)[2], width, height)
    if found is None:
        raise CaptureError(f"The {surface.name} fixture was not found in {geometry(region)}")
    return logical_box(found, region, scale, MARGIN)


def capture_shell(desktop: Desktop, surface: Surface, mode: str, monitor: dict, out: pathlib.Path,
                  work: pathlib.Path, payload: Payload, session: dict) -> dict:
    """The bar (its layer box), or a card fixture: show, locate, grab, close."""
    image = out / f"{surface.name}-{mode}.png"
    bar = desktop.layer(monitor["name"], "omarchy-bar")
    if surface.name == "bar":
        if not bar:
            raise CaptureError("No omarchy-bar layer is mapped")
        desktop.grim(bar, image, monitor["scale"])
        return save_metadata(out, metadata(surface, mode, bar, monitor, image, payload, session,
                                           "hyprctl layers (omarchy-bar)"), [])

    fixture = FIXTURES[surface.name](desktop, monitor["name"])
    region = fixture.region(monitor, bar)
    try:
        fixture.show()
        layer = desktop.wait_layer(monitor["name"], surface.namespace, present=True)
        if layer is None:
            raise CaptureError(f"{surface.namespace} layer never mapped")
        desktop.sleep(0.7)  # fade/slide-in animations
        box = locate_card(desktop, surface, mode, region, monitor["scale"], payload, work)
        desktop.grim(box, image, monitor["scale"])
    finally:
        problems = fixture.close()
    data = save_metadata(out, metadata(
        surface, mode, box, monitor, image, payload, session,
        f"[{surface.section}] background-probe diff inside the {surface.namespace} layer {geometry(layer)}",
        backdrop=backdrop_clients(desktop, box)), problems)
    if problems:
        raise CaptureError("; ".join(problems))
    return data


def run_captures(surfaces: list[Surface], modes: list[str], out: pathlib.Path, desktop: Desktop,
                 dotfiles: pathlib.Path, log: Callable[[str], None] = print,
                 resolve_theme: Callable[[str], pathlib.Path] | None = None,
                 page_url: str | None = None) -> list[dict]:
    """Capture every surface × mode; always restores theme and cursor state."""
    out.mkdir(parents=True, exist_ok=True)
    resolve_theme = resolve_theme or (lambda mode: theme_dir(mode, dotfiles))
    payloads = {mode: build_payload(resolve_theme(mode), desktop.run, OMARCHY_PATH, desktop.home) for mode in modes}
    restore = current_payload(desktop.state)
    monitor = desktop.monitor()
    session = environment(desktop.run, dotfiles)
    cursor = desktop.cursor_setting()
    results = []
    restore_errors = []
    shell_surfaces = [surface for surface in surfaces if surface.name not in WINDOW_SURFACES]
    window_surfaces = [surface for surface in surfaces if surface.name in WINDOW_SURFACES]

    def record(mode: str, label: str, names: list[str], capture: Callable[[], list[dict]]) -> None:
        try:
            for data in capture():
                results.append({"surface": data["surface"], "mode": mode, "ok": True, "metadata": data})
                log(f"captured {out / data['image']}  {geometry(data['box'])} @ {data['scale']:g}x "
                    f"on {data['monitor']}")
        except CaptureError as error:
            results.extend({"surface": name, "mode": mode, "ok": False, "error": str(error)} for name in names)
            log(f"FAILED {label}-{mode}: {error}")

    with tempfile.TemporaryDirectory(prefix="white-pill-capture-", ignore_cleanup_errors=True) as scratch:
        work = pathlib.Path(scratch)
        try:
            desktop.set_cursor_setting(0)
            for mode in modes:
                desktop.apply_theme(payloads[mode])
                desktop.sleep(0.5)
                if window_surfaces:  # one fixture session yields every window surface
                    record(mode, "windows", [surface.name for surface in window_surfaces],
                           lambda: capture_windows(desktop, window_surfaces, mode, out, work, payloads[mode],
                                                   session, resolve_theme(mode), page_url))
                for surface in shell_surfaces:
                    record(mode, surface.name, [surface.name],
                           lambda: [capture_shell(desktop, surface, mode, monitor, out, work, payloads[mode], session)])
        finally:
            for step, action in (("theme", lambda: desktop.apply_theme(restore)),
                                 ("cursor", lambda: desktop.set_cursor_setting(cursor))):
                try:
                    action()
                except Exception as error:  # keep restoring the rest
                    restore_errors.append(f"restore {step}: {error}")
            log(f"restored theme payload from {restore.source} and no_hardware_cursors={cursor}")
    if restore_errors:
        raise CaptureError("; ".join(restore_errors))
    return results


# ------------------------------------------------------------------ comparison

def specimen_scale(native_width: int, css_width: float) -> float:
    """Device scale factor that renders a `css_width` specimen at the native capture's pixel width."""
    if css_width <= 0:
        raise CaptureError("The live specimen has no width")
    return native_width / css_width


def browser_capture(surface: Surface, mode: str, url: str, scale: float, target: pathlib.Path,
                    native_pixels: dict | None = None) -> dict:
    """Screenshot the live specimen in headless Chromium (cdp.py).

    Shell surfaces render at the native device scale with MARGIN around the card. Window
    surfaces are scaled drawings of a whole output or window (the desktop specimen is 1280 CSS px
    for 2560 logical px), so they render with no margin at the device scale that makes the
    specimen as wide as the native PNG.
    """
    selector = surface.specimen_selector(mode)
    windowed = surface.view != "shell"
    viewport = (2400, 1600) if windowed else (1600, 1000)  # the 1280 px desktop specimen must fit horizontally
    margin = 0 if windowed else MARGIN
    try:
        browser = cdp.launch(url.rstrip("/") + f"/#{surface.view}", scale=scale,
                             width=viewport[0], height=viewport[1])
    except cdp.CDPError as error:
        raise CaptureError(str(error)) from error
    measure = f"""(() => {{
      const node = document.querySelector({json.dumps(selector)});
      node.scrollIntoView({{block: "center"}});
      const box = node.getBoundingClientRect();
      return {{x: box.x + scrollX, y: box.y + scrollY, width: box.width, height: box.height}};
    }})()"""
    try:
        browser.wait(f"!!document.querySelector({json.dumps(selector)}) && "
                     "document.querySelectorAll('.token-row').length > 0", "specimen to render", 8)
        browser.evaluate("document.fonts.ready.then(() => true)")
        if windowed:  # filled by studio.js; the browser specimen embeds the fixture page in an iframe
            browser.wait(f"""(() => {{
              const node = document.querySelector({json.dumps(selector)});
              const frames = [...node.querySelectorAll("iframe")];
              return node.offsetWidth > 0 && !node.querySelector("[data-fill]:empty")
                && frames.every(frame => frame.contentDocument?.readyState === "complete");
            }})()""", "window specimen to fill", 8)
            browser.evaluate("document.fonts.ready.then(() => true)")
        rect = browser.evaluate(measure)
        if windowed and native_pixels:
            scale = specimen_scale(native_pixels["width"], rect["width"])
            browser.call("Emulation.setDeviceMetricsOverride", width=viewport[0], height=viewport[1],
                         deviceScaleFactor=scale, mobile=False)
            browser.evaluate("new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))")
            rect = browser.evaluate(measure)
        clip = {"x": max(0, rect["x"] - margin), "y": max(0, rect["y"] - margin),
                "width": rect["width"] + 2 * margin, "height": rect["height"] + 2 * margin}
        browser.screenshot(target, clip=clip)
    except cdp.CDPError as error:
        raise CaptureError(str(error)) from error
    finally:
        browser.close()
    return {"selector": selector, "clip": clip, "device_scale": round(scale, 4)}


def compare_images(native: pathlib.Path, browser: pathlib.Path, run: Runner = run_command) -> dict | None:
    magick = shutil.which("magick")
    if not magick:
        return None
    width, height = png_size(native)
    with tempfile.TemporaryDirectory(prefix="white-pill-rmse-") as scratch:
        resized = pathlib.Path(scratch) / "browser.png"
        checked(run, [magick, str(browser), "-resize", f"{width}x{height}!", str(resized)])
        result = run([magick, "compare", "-metric", "RMSE", str(native), str(resized), "null:"])
    match = re.search(r"([0-9.eE+-]+)\s*\(([0-9.eE+-]+)\)", result.stderr + result.stdout)
    if not match:
        raise CaptureError(f"compare gave no metric: {(result.stderr or result.stdout).strip()}")
    return {"metric": "RMSE", "absolute": float(match.group(1)), "normalized": float(match.group(2)),
            "note": "Browser specimen resized to the native pixel size; backdrops differ, so treat as a trend."}


def compare(surface_name: str, mode: str, out: pathlib.Path, url: str) -> dict:
    surface = SURFACES.get(surface_name)
    if surface is None or mode not in MODES:
        raise CaptureError(f"Unknown surface or mode: {surface_name} {mode}")
    native = out / f"{surface.name}-{mode}.png"
    meta_path = out / f"{surface.name}-{mode}.json"
    if not native.is_file() or not meta_path.is_file():
        raise CaptureError(f"No native capture at {native}; run `script/capture run --surface {surface.name}` first")
    data = json.loads(meta_path.read_text())
    browser = out / f"{surface.name}-{mode}.browser.png"
    details = browser_capture(surface, mode, url, float(data.get("scale", 1)), browser,
                              native_pixels=data.get("pixels"))
    metric = compare_images(native, browser)
    data["comparison"] = {"browser_image": browser.name, **details, **({"result": metric} if metric else {}),
                          "date": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    meta_path.write_text(json.dumps(data, indent=2) + "\n")
    return data["comparison"]


# ------------------------------------------------------------- studio support

def safe_file(directory: pathlib.Path, name: str) -> pathlib.Path | None:
    """A capture file inside `directory`, or None; rejects traversal and other types."""
    if not FILE_RE.match(name):
        return None
    path = (directory / name).resolve()
    if path.parent != directory.resolve() or not path.is_file():
        return None
    return path


def list_captures(directory: pathlib.Path, prefix: str = "/captures/") -> dict:
    captures = []
    if directory.is_dir():
        for meta_path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(meta_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or data.get("surface") not in SURFACES or data.get("mode") not in MODES:
                continue
            image = directory / f"{data['surface']}-{data['mode']}.png"
            browser = directory / f"{data['surface']}-{data['mode']}.browser.png"
            version = int(image.stat().st_mtime) if image.is_file() else 0
            captures.append({
                **data,
                "imageUrl": f"{prefix}{image.name}?v={version}" if image.is_file() else None,
                "browserUrl": f"{prefix}{browser.name}?v={int(browser.stat().st_mtime)}" if browser.is_file() else None,
            })
    return {"ok": True, "surfaces": [dataclasses.asdict(surface) for surface in PLAN], "modes": list(MODES),
            "captures": captures}


def run_request(surface: str, mode: str, script: pathlib.Path, out: pathlib.Path,
                compare_url: str | None = None, timeout: float = 120, page_url: str | None = None) -> dict:
    """Run `script/capture run` for the studio; SIGTERM (not SIGKILL) on timeout so restore runs."""
    if surface not in SURFACES or SURFACES[surface].status != "supported":
        raise CaptureError(f"{surface} cannot be captured: {SURFACES[surface].reason if surface in SURFACES else 'unknown surface'}")
    if mode not in (*MODES, "both"):
        raise CaptureError(f"Unknown mode: {mode}")
    command = [str(script), "run", "--surface", surface, "--mode", mode, "--out", str(out)]
    if compare_url:
        command += ["--compare-url", compare_url]
    if page_url:
        command += ["--page-url", page_url]
    process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            output, _ = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
        raise CaptureError(f"Capture timed out after {timeout:g}s:\n{output.strip()}")
    return {"returncode": process.returncode, "output": output.strip()}


RUN_LOCK = threading.Lock()


def server_paths(server) -> tuple[pathlib.Path, pathlib.Path]:
    """(captures dir, capture script) for a studio server; overridable for tests."""
    source = pathlib.Path(server.source)
    root = source.parent.parent if source.parent.name == "design-system" else source.parent
    directory = getattr(server, "captures_dir", None) or root / "captures"
    return pathlib.Path(directory), root / "script" / "capture"


def send_file(handler, path: pathlib.Path) -> None:
    body = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", "image/png" if path.suffix == ".png" else "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def handle_get(handler, path: str) -> bool:
    """Studio GET routes: /api/captures and read-only /captures/<file>. True when handled."""
    directory, _ = server_paths(handler.server)
    if path == "/api/captures":
        handler.send_json(list_captures(directory))
        return True
    if path.startswith("/captures/"):
        found = safe_file(directory, unquote(path.removeprefix("/captures/")))
        if found is None:
            handler.send_error(404)
        else:
            send_file(handler, found)
        return True
    return False


def handle_post(handler, path: str, payload: dict) -> bool:
    """POST /api/captures/run {surface, mode, compare?}: run the harness, return the new list."""
    if path != "/api/captures/run":
        return False
    directory, script = server_paths(handler.server)
    surface, mode = payload.get("surface"), payload.get("mode", "both")
    if not isinstance(surface, str) or not isinstance(mode, str):
        handler.send_json({"ok": False, "error": "surface and mode must be strings"}, 400)
        return True
    if not script.is_file():
        handler.send_json({"ok": False, "error": f"No capture script at {script}"}, 400)
        return True
    if not RUN_LOCK.acquire(blocking=False):
        handler.send_json({"ok": False, "error": "A capture is already running"}, 409)
        return True
    try:
        host, port = handler.server.server_address[:2]
        studio = f"http://{host}:{port}"
        url = studio if payload.get("compare", True) else None
        result = run_request(surface, mode, script, directory, url,
                             timeout=float(getattr(handler.server, "capture_timeout", 120)),
                             page_url=studio + BROWSER_PAGE_PATH)
    except CaptureError as error:
        handler.send_json({"ok": False, "error": str(error)}, 400)
        return True
    finally:
        RUN_LOCK.release()
    listing = list_captures(directory)
    if result["returncode"]:
        handler.send_json({**listing, "ok": False, "error": result["output"] or "Capture failed",
                           "result": result}, 500)
    else:
        handler.send_json({**listing, "result": result})
    return True


# ------------------------------------------------------------------------- CLI

def print_plan() -> None:
    print(f"{'surface':<14}{'status':<15}{'modes':<12}{'deterministic':<15}method / reason")
    for surface in PLAN:
        detail = surface.method or surface.reason
        if surface.method and surface.reason:
            detail += f"\n{'':<56}note: {surface.reason}"
        print(f"{surface.name:<14}{surface.status:<15}{'light,dark' if surface.status == 'supported' else '-':<12}"
              f"{'yes' if surface.deterministic else 'no':<15}{detail}")


def terminate_gracefully(signum, _frame):
    raise SystemExit(128 + signum)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture native Omarchy reference images")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="print the fixture plan")
    run_parser = commands.add_parser("run", help="capture supported surfaces")
    run_parser.add_argument("--surface", nargs="+", default=["all"], choices=["all", *SURFACES],
                            help="one or more surfaces; windows, terminal, and browser share one fixture session")
    run_parser.add_argument("--mode", default="both", choices=["both", *MODES])
    run_parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    run_parser.add_argument("--compare-url", help="also compare against the live specimen served here")
    run_parser.add_argument("--page-url", help="browser fixture page (default: <compare-url or "
                            f"{DEFAULT_STUDIO_URL}>{BROWSER_PAGE_PATH}, else a local placeholder)")
    compare_parser = commands.add_parser("compare", help="pair a native capture with its browser specimen")
    compare_parser.add_argument("surface", choices=list(SURFACES))
    compare_parser.add_argument("mode", choices=list(MODES))
    compare_parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    compare_parser.add_argument("--url", default="http://127.0.0.1:4777")
    args = parser.parse_args(argv)

    if args.command == "list":
        print_plan()
        return 0

    try:
        if args.command == "compare":
            result = compare(args.surface, args.mode, args.out.resolve(), args.url)
            print(f"browser  {args.out / result['browser_image']}")
            metric = result.get("result")
            print(f"RMSE     {metric['absolute']:.1f} ({metric['normalized']:.4f})" if metric
                  else "RMSE     skipped (ImageMagick not found)")
            return 0

        if "all" in args.surface:
            surfaces = supported()
        else:
            surfaces = []
            for name in dict.fromkeys(args.surface):
                surface = SURFACES[name]
                if surface.status != "supported":
                    print(f"{surface.name} is {surface.status}: {surface.reason}", file=sys.stderr)
                    return 2
                surfaces.append(surface)
        modes = list(MODES) if args.mode == "both" else [args.mode]
        for signum in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, terminate_gracefully)
        page_url = args.page_url or (args.compare_url or DEFAULT_STUDIO_URL).rstrip("/") + BROWSER_PAGE_PATH
        results = run_captures(surfaces, modes, args.out.resolve(), Desktop(), dotfiles_root(), page_url=page_url)
        if args.compare_url:
            for item in results:
                if item["ok"]:
                    try:
                        metric = compare(item["surface"], item["mode"], args.out.resolve(), args.compare_url)
                        print(f"compared {item['surface']}-{item['mode']}: {metric.get('result', {}).get('normalized', 'n/a')}")
                    except Exception as error:  # comparison is optional
                        print(f"compare {item['surface']}-{item['mode']} skipped: {error}")
        return 0 if all(item["ok"] for item in results) else 1
    except CaptureError as error:
        print(f"capture: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
