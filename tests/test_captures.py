import contextlib
import datetime
import io
import json
import pathlib
import shutil
import stat
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zlib
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import captures  # noqa: E402
from studio import StudioHandler  # noqa: E402


def png_bytes(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FakeRunner:
    """Records commands; answers by the longest matching command prefix."""

    def __init__(self, answers: dict[tuple, captures.Result]):
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, command, **_kwargs):
        self.calls.append(list(command))
        for length in range(len(command), 0, -1):
            answer = self.answers.get(tuple(command[:length]))
            if answer is not None:
                return answer
        return captures.Result(1, "", "unexpected command")


TEMPLATE = """\
# comment
[bar]
background = "{{ background }}"
text = "{{ foreground }}"
strip = "{{ accent_strip }}"
rgb = "{{ accent_rgb }}"
dim = "{{ mix foreground background 34% }}"

[hyprland]
active-border = "{{ shell_gradient hyprland_active_border accent }}"

[popups]
background = "{{ background }}"
background-alpha = 1.0
"""


class ShellMergeTest(unittest.TestCase):
    colors = {"background": "#F0EDEC", "foreground": "#2C363C", "accent": "#1853C7"}

    def test_template_substitutions(self):
        text = captures.render_template(TEMPLATE, self.colors)
        self.assertIn('background = "#F0EDEC"', text)
        self.assertIn('strip = "1853C7"', text)
        self.assertIn('rgb = "24,83,199"', text)
        self.assertIn('dim = "#6f7478"', text)  # 34% of the way from foreground to background
        self.assertIn('active-border = "#1853C7"', text)  # gradient key missing → fallback color
        self.assertNotIn("{{", text)

    def test_gradient_with_angle(self):
        colors = {**self.colors, "hyprland_active_border": "accent foreground 45deg"}
        text = captures.render_template(TEMPLATE, colors)
        self.assertIn('active-border = "#1853C7 #2C363C 45deg"', text)

    def test_overlay_replaces_section_in_place(self):
        base = captures.render_template(TEMPLATE, self.colors)
        merged = captures.overlay_section(base, "hyprland", '[hyprland]\nactive-border = "#FFFFFF"\n')
        self.assertIn('[hyprland]\nactive-border = "#FFFFFF"\n[popups]', merged)
        self.assertEqual(merged.count("[hyprland]"), 1)
        self.assertIn('text = "#2C363C"', merged)

    def test_overlay_appends_missing_and_last_sections(self):
        base = "[bar]\na = 1\n"
        appended = captures.overlay_section(base, "tooltip", "[tooltip]\nb = 2\n")
        self.assertEqual(appended, "[bar]\na = 1\n\n[tooltip]\nb = 2\n")
        replaced_last = captures.overlay_section(base, "bar", "[bar]\na = 3\n")
        self.assertEqual(replaced_last, "\n[bar]\na = 3\n")  # awk emits a blank line when the section ran to EOF

    def test_merge_applies_fragments_in_name_order_and_ignores_others(self):
        base = "[bar]\na = 1\n\n[popups]\nb = 1\n"
        merged = captures.merge_shell_toml(base, {
            "shell.popups.toml": "[popups]\nb = 2\n",
            "shell.bar.toml": "[bar]\na = 2\n",
            "colors.toml": "ignored = true\n",
        })
        self.assertEqual(merged, "[bar]\na = 2\n\n[popups]\nb = 2\n")  # popups ran to EOF

    def test_build_payload_uses_theme_color_resolver(self):
        with tempfile.TemporaryDirectory() as directory:
            theme = pathlib.Path(directory)
            (theme / "colors.toml").write_text('background = "#F0EDEC"\n')
            (theme / "shell.popups.toml").write_text('[popups]\nbackground = "#FFFFFF"\n')
            template = theme / "omarchy" / "default" / "themed" / "shell.toml.tpl"
            template.parent.mkdir(parents=True)
            template.write_text(TEMPLATE)
            table = "\n".join(f"{key}\t{value}" for key, value in self.colors.items())
            run = FakeRunner({("omarchy-theme-color",): captures.Result(0, table)})
            payload = captures.build_payload(theme, run, theme / "omarchy", theme / "home")
        self.assertEqual(run.calls[0][:3], ["omarchy-theme-color", "--file", str(theme / "colors.toml")])
        self.assertIn('[popups]\nbackground = "#FFFFFF"', payload.shell)
        self.assertEqual(payload.colors, 'background = "#F0EDEC"\n')
        self.assertEqual(len(payload.digest()["shell_sha256"]), 64)

    def test_probe_payload_only_touches_one_section(self):
        payload = captures.Payload("", captures.render_template(TEMPLATE, self.colors), "theme")
        probe = captures.probe_payload(payload, "popups", "#000000")
        self.assertIn('[popups]\nbackground = "#000000"\nbackground-alpha = 1.0', probe.shell)
        self.assertIn('[bar]\nbackground = "#F0EDEC"', probe.shell)


class PixelTest(unittest.TestCase):
    def test_changed_box_ignores_small_noise(self):
        width, height = 60, 40
        before = bytearray(width * height * 3)
        after = bytearray(before)
        for y in range(10, 20):
            for x in range(15, 45):
                after[(y * width + x) * 3] = 200
        after[(35 * width + 2) * 3] = 255  # a lone changed pixel far away
        box = captures.changed_box(bytes(before), bytes(after), width, height)
        self.assertEqual(box, (15, 10, 30, 10))
        self.assertIsNone(captures.changed_box(bytes(before), bytes(before), width, height))

    def test_logical_box_scales_grows_and_clamps(self):
        region = {"x": 100, "y": 50, "width": 200, "height": 100}
        box = captures.logical_box((30, 15, 60, 30), region, 1.5, 24)
        self.assertEqual(box, {"x": 100, "y": 50, "width": 84, "height": 54})

    def test_read_ppm(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "a.ppm"
            path.write_bytes(b"P6\n# grim\n2 1\n255\n" + bytes([1, 2, 3, 4, 5, 6]))
            self.assertEqual(captures.read_ppm(path), (2, 1, bytes([1, 2, 3, 4, 5, 6])))


class MetadataTest(unittest.TestCase):
    def test_environment_and_metadata_from_injected_commands(self):
        run = FakeRunner({
            ("pacman", "-Q", "quickshell"): captures.Result(0, "quickshell 0.3.1-1\n"),
            ("pacman", "-Q", "qt6-base"): captures.Result(0, "qt6-base 6.11.2-3\n"),
            ("pacman", "-Q", "hyprland"): captures.Result(0, "hyprland 0.56.2-2\n"),
            ("pacman", "-Q", "grim"): captures.Result(1, "", "not installed"),
            ("git", "-C", "/dots", "rev-parse"): captures.Result(0, "a" * 40 + "\n"),
            ("git", "-C", "/dots", "status"): captures.Result(0, " M claude/all.sh\n"),
            ("git", "-C", str(captures.ROOT), "rev-parse"): captures.Result(128, "", "no commits"),
            ("omarchy", "theme", "current"): captures.Result(0, "Mekanikos Light\n"),
            ("fc-match",): captures.Result(0, "Liberation Sans|Regular|/usr/share/fonts/LiberationSans.ttf"),
        })
        session = captures.environment(run, pathlib.Path("/dots"))
        self.assertEqual(session["versions"], {"quickshell": "0.3.1-1", "qt6-base": "6.11.2-3",
                                               "hyprland": "0.56.2-2", "grim": None})
        self.assertEqual(session["dotfiles"], {"path": "/dots", "commit": "a" * 40, "dirty": True})
        self.assertEqual(session["studio"]["commit"], "uncommitted")
        self.assertEqual(len(session["studio"]["tokens_sha256"]), 64)
        self.assertTrue(session["font"]["fallback"])
        self.assertEqual(session["user_theme"], "Mekanikos Light")

        with tempfile.TemporaryDirectory() as directory:
            image = pathlib.Path(directory) / "osd-light.png"
            image.write_bytes(png_bytes(486, 159))
            payload = captures.Payload("colors", "shell", "/dots/omarchy-themes/mekanikos-light")
            data = captures.metadata(
                captures.SURFACES["osd"], "light", {"x": 1, "y": 2, "width": 324, "height": 106},
                {"name": "DP-2", "scale": 1.5}, image, payload, session, "probe",
                now=datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.timezone.utc), backdrop=["ghostty"])
        self.assertEqual(data["date"], "2026-09-25T12:00:00+00:00")
        self.assertEqual(data["pixels"], {"width": 486, "height": 159})
        self.assertEqual(data["scale"], 1.5)
        self.assertEqual(data["monitor"], "DP-2")
        self.assertEqual(data["theme"]["applied"], "/dots/omarchy-themes/mekanikos-light")
        self.assertEqual(data["theme"]["colors_sha256"], captures.Payload("colors", "", "").digest()["colors_sha256"])
        self.assertEqual(data["versions"]["qt6-base"], "6.11.2-3")
        self.assertEqual(data["backdrop"], ["ghostty"])
        json.dumps(data)

    def test_plan_marks_unsupported_surfaces_with_reasons(self):
        statuses = {surface.name: surface.status for surface in captures.PLAN}
        self.assertEqual(statuses, {"osd": "supported", "notification": "supported", "bar": "supported",
                                    "windows": "supported", "terminal": "supported", "browser": "supported",
                                    "tooltip": "needs-fixture", "audio-panel": "needs-fixture", "hunk": "manual"})
        for surface in captures.PLAN:
            if surface.status != "supported":
                self.assertTrue(surface.reason)
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(captures.main(["run", "--surface", "tooltip"]), 2)
        self.assertIn("needs-fixture", stderr.getvalue())


class RestoreTest(unittest.TestCase):
    """run_captures must restore theme and cursor even when a capture step explodes."""

    def test_restores_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            current = home / ".local" / "state" / "omarchy" / "current" / "theme"
            current.mkdir(parents=True)
            (current / "colors.toml").write_text("user colors\n")
            (current / "shell.toml").write_text("[bar]\n")
            dotfiles = home / "dots"
            for name in ("mekanikos", "mekanikos-light"):
                theme = dotfiles / "omarchy-themes" / name
                theme.mkdir(parents=True)
                (theme / "colors.toml").write_text(f"{name}\n")
                (theme / "shell.toml").write_text("[popups]\nbackground = \"#FFFFFF\"\n")
            monitors = json.dumps([{"name": "DP-2", "x": 0, "y": 0, "width": 3840, "height": 2160,
                                    "scale": 1.5, "focused": True}])
            run = FakeRunner({
                ("hyprctl", "monitors"): captures.Result(0, monitors),
                ("hyprctl", "layers"): captures.Result(0, "{}"),
                ("hyprctl", "getoption"): captures.Result(0, '{"int": 2}'),
                ("hyprctl", "eval"): captures.Result(0, "ok"),
                ("omarchy", "shell", "shell", "applyTheme"): captures.Result(0, "ok"),
                ("omarchy", "osd"): captures.Result(0, ""),
                ("omarchy", "shell", "osd", "close"): captures.Result(0, "ok"),
                ("grim",): captures.Result(1, "", "compositor went away"),
            })
            desktop = captures.Desktop(run, sleep=lambda _seconds: None, home=home)
            results = captures.run_captures([captures.SURFACES["osd"]], ["light"], home / "out", desktop,
                                            dotfiles, log=lambda _line: None,
                                            resolve_theme=lambda mode: dotfiles / "omarchy-themes" / captures.THEME_DIRS[mode])
        self.assertFalse(results[0]["ok"])
        applied = [call for call in run.calls if call[:4] == ["omarchy", "shell", "shell", "applyTheme"]]
        import base64
        self.assertEqual(base64.b64decode(applied[-1][4]).decode(), "user colors\n")
        cursor_calls = [call for call in run.calls if call[:2] == ["hyprctl", "eval"]]
        self.assertIn("no_hardware_cursors = 0", cursor_calls[0][2])
        self.assertIn("no_hardware_cursors = 2", cursor_calls[-1][2])

    def test_notification_history_is_restored_after_trim(self):
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            history = home / ".local" / "state" / "omarchy" / "notifications" / "history"
            images = history.parent / "images"
            history.mkdir(parents=True)
            images.mkdir()
            for index in range(10):
                (history / f"{1000 + index}-{index}.json").write_text(json.dumps({"summary": f"real {index}"}))
            (images / "1000-0-avatar.png").write_bytes(b"img")

            def dismiss(command, **_kwargs):
                if command[:4] == ["omarchy", "shell", "notifications", "dismiss"]:
                    # the shell archives the fixture and trims the oldest entry
                    (history / "2000-99.json").write_text(json.dumps(
                        {"summary": captures.FIXTURE_SUMMARY, "app": captures.FIXTURE_APP}))
                    (history / "1000-0.json").unlink()
                    (images / "1000-0-avatar.png").unlink()
                if command[:2] == ["hyprctl", "layers"]:
                    return captures.Result(0, "{}")
                if command[:4] == ["omarchy", "shell", "notifications", "isDnd"]:
                    return captures.Result(0, "off")
                return captures.Result(0, "")

            fixture = captures.NotificationFixture(captures.Desktop(dismiss, sleep=lambda _s: None, home=home), "DP-2")
            fixture.show()
            problems = fixture.close()
            names = sorted(path.name for path in history.iterdir())
        self.assertEqual(problems, [])
        self.assertEqual(names, sorted(f"{1000 + index}-{index}.json" for index in range(10)))

    def test_notification_that_never_reaches_history_is_reported_and_history_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            history = home / ".local" / "state" / "omarchy" / "notifications" / "history"
            history.mkdir(parents=True)
            (history / "1000-0.json").write_text(json.dumps({"summary": "real"}))
            run = FakeRunner({("hyprctl", "layers"): captures.Result(0, "{}"),
                              ("omarchy", "shell", "notifications"): captures.Result(0, ""),
                              ("omarchy", "notification", "send"): captures.Result(0, "")})
            fixture = captures.NotificationFixture(captures.Desktop(run, sleep=lambda _s: None, home=home), "DP-2")
            fixture.show()
            problems = fixture.close()
            backup_left = fixture.backup.exists()
            names = [path.name for path in history.iterdir()]
        self.assertEqual(len(problems), 1)
        self.assertIn("did not reach history", problems[0])
        self.assertEqual(names, ["1000-0.json"])
        self.assertFalse(backup_left)
        dismissals = [call for call in run.calls if call[:4] == ["omarchy", "shell", "notifications", "dismiss"]]
        self.assertEqual(len(dismissals), 3)
        self.assertNotIn(["omarchy", "shell", "notifications", "clear"], run.calls)

    def test_cursor_setting_falls_back_to_keyword(self):
        run = FakeRunner({("hyprctl", "eval"): captures.Result(0, "error: unknown key"),
                          ("hyprctl", "keyword"): captures.Result(0, "ok")})
        captures.Desktop(run, sleep=lambda _s: None).set_cursor_setting(1)
        self.assertEqual(run.calls[-1], ["hyprctl", "keyword", "cursor:no_hardware_cursors", "1"])

    def test_dnd_refuses_notification(self):
        run = FakeRunner({("omarchy", "shell", "notifications", "isDnd"): captures.Result(0, "on")})
        fixture = captures.NotificationFixture(captures.Desktop(run, sleep=lambda _s: None), "DP-2")
        with self.assertRaises(captures.CaptureError):
            fixture.show()
        self.assertEqual(fixture.close(), [])


THEME_LUA = """\
local focus_blue = "rgb(1853c7)"
local transparent = "rgba(55555588)" -- trailing comment
--[[ block
comment ]]
hl.config({
	general = {
		border_size = 4,
		gaps_out = 12,
		col = { active_border = focus_blue, inactive_border = transparent },
	},
	decoration = {
		rounding = 14,
		shadow = { color = "rgba(10131a18)", offset = { 0, 4 } },
	},
	plugin = { borders_plus_plus = { add_borders = 0 } },
})
hl.layer_rule({ match = { namespace = "^x$" }, blur = true })
o.window(".*", { tag = "-default-opacity", opacity = "1 1", no_blur = true })
local ignored = os.getenv("HOME")
"""


def option(**value):
    return {"option": "x", "set": True, **value}


class WindowChromeTest(unittest.TestCase):
    def test_parse_theme_lua_resolves_locals_and_rules(self):
        theme = captures.parse_theme_lua(THEME_LUA)
        self.assertEqual(theme["config"]["general"]["col"], {"active_border": "rgb(1853c7)",
                                                             "inactive_border": "rgba(55555588)"})
        self.assertEqual(theme["config"]["decoration"]["shadow"]["offset"], [0, 4])
        self.assertEqual(theme["windows"], [{"match": ".*", "rules": {"tag": "-default-opacity", "opacity": "1 1",
                                                                      "no_blur": True}}])
        flat = captures.flatten_config(theme["config"])
        self.assertIn(("general", "col", "active_border"), flat)
        self.assertNotIn("plugin", {path[0] for path in flat})
        self.assertEqual(captures.option_name(("general", "col", "active_border")), "general:col.active_border")
        self.assertEqual(captures.option_name(("decoration", "shadow", "color")), "decoration:shadow:color")
        self.assertEqual(captures.window_opacity("terminal", theme), ("1", "1"))
        self.assertEqual(captures.window_opacity("browser", {"windows": []}), ("1.0", "0.985"))

    def test_live_values_round_trip_to_lua(self):
        self.assertEqual(captures.live_value(option(gradient="22ffffff 0deg")), "rgba(ffffff22)")
        self.assertEqual(captures.live_value(option(gradient="d10131a 0deg")), "rgba(10131a0d)")  # dropped zero
        self.assertEqual(captures.live_value(option(gradient="ff1853c7 ff2c363c 45deg")),
                         {"colors": ["rgba(1853c7ff)", "rgba(2c363cff)"], "angle": 45})
        self.assertEqual(captures.live_value(option(css="4 12 12 12")), {"top": 4, "right": 12, "bottom": 12, "left": 12})
        self.assertEqual(captures.live_value(option(css="2 2 2 2")), 2)
        self.assertEqual(captures.live_value(option(vec2=[0, 4])), [0, 4])
        restore = {("general", "col", "inactive_border"): "rgba(ffffff22)", ("decoration", "shadow", "offset"): [0, 4]}
        lua = captures.lua_literal(captures.nest(restore))
        self.assertEqual(lua, '{ general = { col = { inactive_border = "rgba(ffffff22)" } }, '
                              'decoration = { shadow = { offset = { 0, 4 } } } }')
        self.assertEqual(captures.canonical("rgb(1853c7)"), captures.canonical(captures.live_value(
            option(gradient="ff1853c7 0deg"))))

    def test_chrome_delta_skips_equal_and_user_overridden_options(self):
        light = captures.parse_theme_lua(THEME_LUA.replace("rgba(55555588)", "rgba(ffffff22)"))
        dark = captures.parse_theme_lua(THEME_LUA.replace("gaps_out = 12", "gaps_out = 20"))
        live = {"general:col.inactive_border": option(gradient="22ffffff 0deg"),
                "general:gaps_out": option(css="4 12 12 12")}  # looknfeel.lua overrides the theme's gaps_out
        delta, skipped = captures.chrome_delta(dark, light, live)
        self.assertEqual(delta, {("general", "col", "inactive_border"): "rgba(55555588)"})
        self.assertEqual(skipped, ["general:gaps_out: user config overrides the theme value"])
        self.assertEqual(captures.chrome_delta(light, light, live), ({}, []))

    def test_crop_geometry(self):
        client = {"at": [20016, 37], "size": [1258, 1387]}
        box = captures.client_box(client, 4)
        self.assertEqual(box, {"x": 20012, "y": 33, "width": 1266, "height": 1395})
        relative = captures.relative_box(box, {"x": 20000, "y": 0})
        self.assertEqual(relative, {"x": 12, "y": 33, "width": 1266, "height": 1395})
        # grim truncates: 33 * 1.5 = 49.5 -> 49, 1395 * 1.5 = 2092.5 -> 2092 (the PNG it writes)
        self.assertEqual(captures.pixel_box(relative, 1.5), {"x": 18, "y": 49, "width": 1899, "height": 2092})
        windows = captures.split_windows([
            {"class": "helium", "at": [21286, 37]}, {"class": "com.mitchellh.ghostty", "at": [20016, 37]}])
        self.assertEqual(windows["terminal"]["at"][0], 20016)
        with self.assertRaises(captures.CaptureError):
            captures.split_windows([{"class": "helium", "at": [0, 0]}, {"class": "com.mitchellh.ghostty", "at": [9, 0]}])
        self.assertEqual(captures.specimen_scale(1899, 633), 3.0)
        self.assertEqual(captures.SURFACES["terminal"].specimen_selector("dark"),
                         "#windows-view .desktop-specimen.dark-preview .hypr-window.is-terminal")
        self.assertEqual(captures.SURFACES["osd"].specimen_selector("light"), "#shell-view .light-preview .osd-specimen")

    def test_ghostty_user_config_swaps_only_the_theme_include(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = pathlib.Path(directory)
            (config_dir / "config").write_text(
                'config-file = ?"~/.local/state/omarchy/current/theme/ghostty.conf"\n\nfont-size = 12\n'
                'config-file = ?"~/.dotfiles/ghostty/config"\nconfig-file = local.conf\n')
            text = captures.ghostty_user_config(config_dir, pathlib.Path("/work/theme.conf"))
        self.assertEqual(text, f'config-file = "/work/theme.conf"\n\nfont-size = 12\n'
                               f'config-file = ?"~/.dotfiles/ghostty/config"\nconfig-file = "{config_dir}/local.conf"\n')
        shown = captures.parse_ghostty_config("font-family = A\nfont-family = B\nfont-size = 16\n# x = y\n")
        self.assertEqual(shown, {"font-family": ["A", "B"], "font-size": "16"})

    @unittest.skipUnless(shutil.which("omarchy-theme-color")
                         and (pathlib.Path.home() / ".local/state/omarchy/current/theme/ghostty.conf").is_file(),
                         "needs an installed Omarchy theme")
    def test_ghostty_conf_matches_installed_theme_byte_for_byte(self):
        current = pathlib.Path.home() / ".local/state/omarchy/current/theme"
        dotfiles = captures.dotfiles_root()
        matched = False
        for mode in captures.MODES:
            directory = dotfiles / "omarchy-themes" / captures.THEME_DIRS[mode]
            if (directory / "colors.toml").is_file() and \
                    (directory / "colors.toml").read_bytes() == (current / "colors.toml").read_bytes():
                generated = captures.themed_file(directory, "ghostty.conf", captures.run_command,
                                                 pathlib.Path("/usr/share/omarchy"), pathlib.Path.home())
                self.assertEqual(generated.encode(), (current / "ghostty.conf").read_bytes())
                matched = True
        if not matched:
            self.skipTest("the installed theme is not one of this repo's generated themes")

    def test_window_metadata_assembly(self):
        fixture = captures.WindowsFixture.__new__(captures.WindowsFixture)
        fixture.output = {"name": "WHITEPILL-CAPTURE", "x": 20000, "y": 0, "width": 3840, "height": 2160,
                          "scale": 1.5, "activeWorkspace": {"id": 3}, "reserved": [0, 29, 0, 0]}
        fixture.browser_command = "helium-browser"
        fixture.info = {"theme_delta": {"general:col.inactive_border": "rgba(55555588)"},
                        "window_props_set": {"terminal": {"opacity": "1"}},
                        "ghostty_effective": {"font-family": ["Iosevka Nerd Font Mono", "IBM Plex Mono"],
                                              "font-size": "16"},
                        "browser_version": "Chrome/154", "browser_flags": ["--no-first-run"],
                        "page": {"url": "http://x/fixtures/browser-page.html?scheme=dark", "source": "studio"}}
        windows = {"terminal": {"class": "com.mitchellh.ghostty", "at": [20016, 37], "size": [1258, 1387]},
                   "browser": {"class": "helium", "at": [21286, 37], "size": [1258, 1387]}}
        options = {"general:border_size": option(int=4), "general:col.active_border": option(gradient="ff1853c7 0deg")}
        run = FakeRunner({
            ("ghostty", "--version"): captures.Result(0, "Ghostty 1.3.1-arch2\n\nVersion\n"),
            ("fc-match",): captures.Result(0, "Iosevka Nerd Font Mono|Regular|/f.ttf"),
            ("pacman", "-Q", "helium-browser-bin"): captures.Result(0, "helium-browser-bin 0.18.1.1-1\n"),
        })
        data = captures.window_metadata(fixture, windows, options, {"terminal": {"rounding": "14"}}, run)
        self.assertEqual(data["output"], {"name": "WHITEPILL-CAPTURE", "headless": True, "size": [3840, 2160],
                                          "scale": 1.5, "workspace": 3, "reserved": [0, 29, 0, 0]})
        self.assertEqual(data["windows"]["browser"]["box"], {"x": 1282, "y": 33, "width": 1266, "height": 1395})
        self.assertEqual(data["windows"]["terminal"]["props"], {"rounding": "14"})
        self.assertEqual(data["hyprland"], {"general:border_size": 4, "general:col.active_border": "rgba(1853c7ff)"})
        self.assertEqual(data["ghostty"]["version"], "Ghostty 1.3.1-arch2")
        self.assertEqual(len(data["ghostty"]["fonts"]), 2)
        self.assertEqual(data["browser"]["package"], "helium-browser-bin 0.18.1.1-1")
        self.assertFalse(data["browser"]["chrome"]["follows_mode"])
        self.assertEqual(data["theme_delta"], {"general:col.inactive_border": "rgba(55555588)"})
        json.dumps(data)

    def test_close_removes_output_and_restores_options(self):
        state = {"output": True, "inactive": "55555588"}

        def run(command, **_kwargs):
            if command[:2] == ["hyprctl", "monitors"]:
                return captures.Result(0, json.dumps([{"name": "DP-2"}] + (
                    [{"name": captures.HEADLESS_OUTPUT, "id": 7}] if state["output"] else [])))
            if command[:3] == ["hyprctl", "output", "remove"]:
                state["output"] = False
                return captures.Result(0, "ok")
            if command[:2] == ["hyprctl", "eval"]:
                if "inactive_border" in command[2]:
                    state["inactive"] = "22ffffff"
                return captures.Result(0, "ok")
            if command[:2] == ["hyprctl", "getoption"]:
                return captures.Result(0, json.dumps({"option": command[2], "gradient": f"{state['inactive']} 0deg"}))
            if command[:2] == ["hyprctl", "activewindow"]:
                return captures.Result(0, json.dumps({"address": "0xuser", "monitor": 0}))
            return captures.Result(1, "", "unexpected")

        desktop = captures.Desktop(run, sleep=lambda _s: None)
        with tempfile.TemporaryDirectory() as directory:
            fixture = captures.WindowsFixture(desktop, "dark", pathlib.Path(directory), pathlib.Path(directory),
                                              pathlib.Path(directory))
            fixture.created_output = True
            fixture.output = {"id": 7}
            fixture.focus = {"address": "0xuser", "class": "ghostty", "workspace": 1}
            fixture.option_snapshot = {("general", "col", "inactive_border"): option(gradient="22ffffff 0deg")}
            steps, problems = fixture.close()
            self.assertEqual(fixture.close(), ([], []))  # idempotent
        self.assertEqual(problems, [])
        self.assertFalse(state["output"])
        self.assertEqual(state["inactive"], "22ffffff")
        self.assertTrue(any("removed headless output" in step for step in steps))
        self.assertTrue(any("restored general:col.inactive_border" in step for step in steps))
        self.assertIn("focus untouched (fixture windows opened silently)", steps)

    def test_run_accepts_several_surfaces_and_rejects_unsupported(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(captures.main(["run", "--surface", "terminal", "hunk"]), 2)

    def test_fixture_files_exist(self):
        self.assertTrue(captures.TERMINAL_SCRIPT.is_file())
        self.assertTrue(captures.TERMINAL_SCRIPT.stat().st_mode & stat.S_IXUSR)
        self.assertIn("terminal-session.ans", captures.TERMINAL_SCRIPT.read_text())
        self.assertTrue(captures.BROWSER_PLACEHOLDER.is_file())



def ppm_bytes(width: int, height: int, box=None) -> bytes:
    """A white PPM, with `box` (x, y, w, h) painted black."""
    pixels = bytearray(b"\xff" * (width * height * 3))
    if box:
        x0, y0, w, h = box
        for y in range(y0, y0 + h):
            pixels[(y * width + x0) * 3:(y * width + x0 + w) * 3] = bytes(w * 3)
    return f"P6\n{width} {height}\n255\n".encode() + bytes(pixels)


class ShellPipelineTest(unittest.TestCase):
    """The OSD card: show, probe-locate, grab, close, restore theme and cursor."""

    def test_osd_capture_end_to_end(self):
        import base64
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            current = home / ".local" / "state" / "omarchy" / "current" / "theme"
            current.mkdir(parents=True)
            (current / "colors.toml").write_text("user colors\n")
            (current / "shell.toml").write_text("[bar]\n")
            theme = home / "dots" / "omarchy-themes" / "mekanikos-light"
            theme.mkdir(parents=True)
            (theme / "colors.toml").write_text("light colors\n")
            (theme / "shell.toml").write_text('[popups]\nbackground = "#FFFFFF"\nbackground-alpha = 0.9\n')
            state = {"osd": False, "shell": "", "cursor": 2}
            applied, grabs = [], []

            def run(command, **_kwargs):
                if command[:2] == ["hyprctl", "monitors"]:
                    return captures.Result(0, json.dumps([{"name": "DP-2", "x": 0, "y": 0, "width": 3840,
                                                           "height": 2160, "scale": 1.5, "focused": True}]))
                if command[:2] == ["hyprctl", "layers"]:
                    levels = [{"namespace": "omarchy-bar", "x": 0, "y": 0, "w": 2560, "h": 29}]
                    if state["osd"]:
                        levels.append({"namespace": "omarchy-osd", "x": 0, "y": 0, "w": 2560, "h": 1440})
                    return captures.Result(0, json.dumps({"DP-2": {"levels": {"2": levels}}}))
                if command[:3] == ["hyprctl", "getoption", "cursor:no_hardware_cursors"]:
                    return captures.Result(0, json.dumps({"int": state["cursor"]}))
                if command[:2] == ["hyprctl", "eval"]:
                    state["cursor"] = int(command[2].split("no_hardware_cursors = ")[1].split()[0])
                    return captures.Result(0, "ok")
                if command[:4] == ["omarchy", "shell", "shell", "applyTheme"]:
                    state["shell"] = base64.b64decode(command[5]).decode()
                    applied.append(base64.b64decode(command[4]).decode())
                    return captures.Result(0, "ok")
                if command[:2] == ["omarchy", "osd"]:
                    state["osd"] = True
                    return captures.Result(0, "")
                if command[:4] == ["omarchy", "shell", "osd", "close"]:
                    state["osd"] = False
                    return captures.Result(0, "ok")
                if command[0] == "grim":
                    path = pathlib.Path(command[-1])
                    grabs.append(command[2])
                    if "ppm" in command:
                        probe = 'background = "#000000"' in state["shell"]
                        path.write_bytes(ppm_bytes(1500, 540, (600, 300, 300, 150) if probe else None))
                    else:
                        path.write_bytes(png_bytes(4, 2))
                    return captures.Result(0, "")
                if command[:2] == ["hyprctl", "clients"]:
                    return captures.Result(0, json.dumps([{"class": "ghostty", "workspace": {"id": 1},
                                                           "at": [1000, 1000], "size": [400, 400]}]))
                if command[:2] == ["hyprctl", "activeworkspace"]:
                    return captures.Result(0, json.dumps({"id": 1}))
                return captures.Result(1, "", "unexpected command")

            desktop = captures.Desktop(run, sleep=lambda _s: None, home=home)
            out = home / "out"
            results = captures.run_captures([captures.SURFACES["osd"]], ["light"], out, desktop, home / "dots",
                                            log=lambda _line: None, resolve_theme=lambda _mode: theme)
            data = json.loads((out / "osd-light.json").read_text())
            leftovers = sorted(path.name for path in out.iterdir())
        self.assertTrue(results[0]["ok"], results)
        # region: 1000x360 logical at the bottom centre (780, 1080); probe box 600,300 300x150 px at 1.5
        self.assertEqual(data["box"], {"x": 1156, "y": 1256, "width": 248, "height": 148})
        self.assertEqual(grabs, ["780,1080 1000x360", "780,1080 1000x360", "1156,1256 248x148"])
        self.assertEqual(data["located_by"], "[popups] background-probe diff inside the omarchy-osd layer "
                                             "0,0 2560x1440")
        self.assertEqual(data["backdrop"], ["ghostty"])
        self.assertEqual(data["pixels"], {"width": 4, "height": 2})
        self.assertNotIn("restore_problems", data)
        self.assertEqual(leftovers, ["osd-light.json", "osd-light.png"])
        # mode payload, probe, mode payload again, then the user's payload
        self.assertEqual(applied, ["light colors\n"] * 3 + ["user colors\n"])
        self.assertEqual(state, {"osd": False, "shell": "[bar]\n", "cursor": 2})


class WindowsPipelineTest(unittest.TestCase):
    """A whole windows session with injected commands: headless output, chrome delta, launches, grabs, restore."""

    def setUp(self):
        import subprocess
        self.processes = [subprocess.Popen(["sleep", "60"]) for _ in range(2)]

    def tearDown(self):
        for process in self.processes:
            process.kill()
            process.wait()

    def fake_hyprland(self, home: pathlib.Path):
        import re
        output_name = captures.HEADLESS_OUTPUT
        state = {"output": False, "clients": [], "inactive": "22ffffff", "timeout": 0.0, "evals": [],
                 "dispatches": [], "launched": []}
        pids = iter(process.pid for process in self.processes)

        def monitors():
            found = [{"name": "DP-2", "id": 0, "x": 0, "y": 0, "width": 3840, "height": 2160, "scale": 1.5,
                      "focused": True}]
            if state["output"]:
                found.append({"name": output_name, "id": 7, "x": 20000, "y": 0, "width": 3840, "height": 2160,
                              "scale": 1.5, "activeWorkspace": {"id": 5}, "reserved": [0, 29, 0, 0]})
            return found

        def run(command, **_kwargs):
            ok = captures.Result(0, "ok")
            if command[:2] == ["hyprctl", "monitors"]:
                return captures.Result(0, json.dumps(monitors()))
            if command[:2] == ["hyprctl", "activewindow"]:
                return captures.Result(0, json.dumps({"address": "0xuser", "class": "firefox", "monitor": 0}))
            if command[:2] == ["hyprctl", "activeworkspace"]:
                return captures.Result(0, json.dumps({"id": 1}))
            if command[:2] == ["hyprctl", "layers"]:
                return captures.Result(0, json.dumps({output_name: {"levels": {"2": [
                    {"namespace": "omarchy-bar", "x": 20000, "y": 0, "w": 2560, "h": 29}]}}}))
            if command[:2] == ["hyprctl", "getoption"]:
                name = command[2]
                if name == "general:col.inactive_border":
                    return captures.Result(0, json.dumps({"option": name, "gradient": f"{state['inactive']} 0deg"}))
                if name.startswith("general:col."):
                    return captures.Result(0, json.dumps({"option": name, "gradient": "ff1853c7 0deg"}))
                if name == "cursor:inactive_timeout":
                    return captures.Result(0, json.dumps({"option": name, "float": state["timeout"]}))
                if name == "general:border_size":
                    return captures.Result(0, json.dumps({"option": name, "int": 4}))
                return captures.Result(0, json.dumps({"option": name, "int": 0}))
            if command[:2] == ["hyprctl", "eval"]:
                code = command[2]
                state["evals"].append(code)
                border = re.search(r'inactive_border = "rgba\((\w{6})(\w{2})\)"', code)
                if border:
                    state["inactive"] = border.group(2) + border.group(1)
                timeout = re.search(r"inactive_timeout = ([0-9.]+)", code)
                if timeout:
                    state["timeout"] = float(timeout.group(1))
                return ok
            if command[:4] == ["hyprctl", "output", "create", "headless"]:
                state["output"] = True
                return ok
            if command[:3] == ["hyprctl", "output", "remove"]:
                state["output"] = False
                return ok
            if command[:2] == ["hyprctl", "dispatch"]:
                state["dispatches"].append(command[2])
                launch = re.match(r'hl\.dsp\.exec_cmd\("([^"]+)", \{ workspace = "5 silent" \}\)', command[2])
                if launch:
                    script = pathlib.Path(launch.group(1)).read_text()
                    state["launched"].append(script)
                    terminal = "ghostty" in script.split("exec ", 1)[1].split()[0]
                    state["clients"].append({
                        "class": "com.mitchellh.ghostty" if terminal else "helium", "pid": next(pids),
                        "address": "0xterm" if terminal else "0xweb", "workspace": {"id": 5},
                        "at": [20016, 37] if terminal else [21286, 37], "size": [1258, 1387]})
                return ok
            if command[:2] == ["hyprctl", "clients"]:
                return captures.Result(0, json.dumps(state["clients"]))
            if command[:2] == ["hyprctl", "getprop"]:
                return captures.Result(0, "1\n")
            if command[0] == "grim":
                pathlib.Path(command[-1]).write_bytes(png_bytes(6, 4) if command[1] == "-o" else png_bytes(3, 2))
                return captures.Result(0, "")
            if command[:2] == ["env", f"XDG_CONFIG_HOME={home / 'work' / 'windows-dark' / 'ghostty-xdg'}"]:
                return captures.Result(0, "font-family = Iosevka\nfont-size = 12\n")
            if command[:2] == ["ghostty", "--version"]:
                return captures.Result(0, "Ghostty 1.3.1\n")
            return captures.Result(1, "", "unexpected command")

        return state, run

    def test_windows_session_end_to_end(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            installed = home / ".local" / "state" / "omarchy" / "current" / "theme"
            mode_dir = home / "theme-dark"
            for path, border in ((installed, "rgba(ffffff22)"), (mode_dir, "rgba(55555588)")):
                path.mkdir(parents=True)
                (path / "hyprland.lua").write_text(
                    f'hl.config({{ general = {{ col = {{ inactive_border = "{border}" }} }} }})\n'
                    'o.window(".*", { opacity = "1 1" })\n')
            (mode_dir / "ghostty.conf").write_text("background = #1c1917\n")
            (mode_dir / "chromium.theme").write_text("28,25,23")
            policy = home / "policy"
            policy.mkdir()
            (policy / "color.json").write_text('{"BrowserThemeColor": "#1c1917", "BrowserColorScheme": "dark"}')
            state, run = self.fake_hyprland(home)
            desktop = captures.Desktop(run, sleep=lambda _s: None, home=home)
            out = home / "out"
            out.mkdir()
            payload = captures.Payload("colors", "shell", str(mode_dir))
            session = {"versions": {}, "dotfiles": {}, "studio": {}, "user_theme": "Mekanikos", "font": {}}
            with mock.patch.object(captures, "browser_command", return_value="helium-browser"), \
                    mock.patch.object(captures, "browser_policy_override", return_value=policy), \
                    mock.patch.object(captures, "wait_for_page", return_value="Chrome/150"):
                results = captures.capture_windows(
                    desktop, [captures.SURFACES[name] for name in ("windows", "terminal", "browser")], "dark", out,
                    home / "work", payload, session, mode_dir, page_url="http://127.0.0.1:9/fixtures/browser-page.html")
            profile_left = (home / "work" / "windows-dark" / "browser-profile").exists()
        alive = [process.poll() is None for process in self.processes]
        self.assertEqual([data["surface"] for data in results], ["windows", "terminal", "browser"])
        windows, terminal, browser = results
        self.assertEqual(windows["box"], {"x": 0, "y": 0, "width": 2560, "height": 1440})
        self.assertEqual(terminal["box"], {"x": 12, "y": 33, "width": 1266, "height": 1395})
        self.assertEqual(terminal["crop_pixels"], {"x": 18, "y": 49, "width": 1899, "height": 2092})
        self.assertEqual(browser["crop_of"], "windows-dark.png")
        self.assertEqual(terminal["located_by"], "hyprctl clients -j (com.mitchellh.ghostty) grown by border_size")
        self.assertEqual(windows["theme_delta"], {"general:col.inactive_border": "rgba(55555588)"})
        self.assertEqual(windows["window_props_set"]["terminal"],
                         {"opacity": "1", "opacity_inactive": "1", "inactive_border_color": "rgba(1853c7ff)"})
        self.assertEqual(windows["window_props_set"]["browser"], {"opacity": "1", "opacity_inactive": "1"})
        self.assertEqual(windows["browser"]["page"]["source"], "placeholder")
        self.assertTrue(windows["browser"]["chrome"]["follows_mode"])
        self.assertEqual(windows["ghostty"]["effective"], {"font-family": "Iosevka", "font-size": "12"})
        self.assertEqual(windows["output"]["workspace"], 5)
        for data in results:
            self.assertNotIn("restore_problems", data)
            json.dumps(data)
        steps = windows["restore"]
        self.assertEqual(len(steps), 9, steps)
        self.assertTrue(steps[0].startswith("applied general:col.inactive_border"))
        self.assertTrue(steps[1].startswith("restored cursor:inactive_timeout=0"))
        self.assertTrue(steps[2].startswith("closed terminal fixture"))
        self.assertTrue(steps[3].startswith("closed browser fixture"))
        self.assertEqual(steps[4:6], ["removed the throwaway browser profile", "stopped the placeholder page server"])
        self.assertTrue(steps[6].startswith(f"removed headless output {captures.HEADLESS_OUTPUT}"))
        self.assertEqual(steps[7], "restored general:col.inactive_border (verified with getoption)")
        self.assertEqual(steps[8], "focus untouched (fixture windows opened silently)")
        # the machine is back where it started
        self.assertFalse(state["output"])
        self.assertEqual(state["inactive"], "22ffffff")
        self.assertEqual(state["timeout"], 0.0)
        self.assertEqual(alive, [False, False])
        self.assertFalse(profile_left)
        # hl.monitor rule, chrome delta, cursor hide, cursor restore, option restore
        self.assertTrue(state["evals"][0].startswith("hl.config({ general"))
        self.assertIn(f'output = "{captures.HEADLESS_OUTPUT}", mode = "3840x2160@60", position = "20000x0"',
                      state["evals"][1])
        self.assertEqual(len(state["evals"]), 5)
        terminal_script, browser_script = state["launched"]
        self.assertIn("--config-default-files=false", terminal_script)
        self.assertTrue(browser_script.startswith("#!/bin/sh\nexec unshare --user"))
        self.assertIn("?scheme=dark", browser_script)
        self.assertEqual(sum("set_prop" in item for item in state["dispatches"]), 5)
        self.assertFalse(any("hl.dsp.focus" in item for item in state["dispatches"]))

    def test_failed_grab_still_restores_everything(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            for path in (home / ".local" / "state" / "omarchy" / "current" / "theme", home / "theme-dark"):
                path.mkdir(parents=True)
                (path / "hyprland.lua").write_text("\n")
            (home / "theme-dark" / "ghostty.conf").write_text("\n")
            (home / "theme-dark" / "chromium.theme").write_text("28,25,23")
            state, fake = self.fake_hyprland(home)

            def run(command, **kwargs):
                if command[0] == "grim":
                    return captures.Result(1, "", "compositor went away")
                return fake(command, **kwargs)

            desktop = captures.Desktop(run, sleep=lambda _s: None, home=home)
            with mock.patch.object(captures, "browser_command", return_value="helium-browser"), \
                    mock.patch.object(captures, "browser_policy_override", return_value=None), \
                    mock.patch.object(captures, "wait_for_page", return_value="Chrome/150"), \
                    self.assertRaises(captures.CaptureError) as caught:
                captures.capture_windows(desktop, [captures.SURFACES["windows"]], "dark", home / "out",
                                         home / "work", captures.Payload("", "", ""), {}, home / "theme-dark",
                                         page_url="http://127.0.0.1:9/fixtures/browser-page.html")
        self.assertIn("compositor went away", str(caught.exception))
        self.assertNotIn("restore problems", str(caught.exception))
        self.assertFalse(state["output"])
        self.assertEqual(state["timeout"], 0.0)
        self.assertEqual([process.poll() is None for process in self.processes], [False, False])
        self.assertTrue(state["launched"][1].startswith("#!/bin/sh\nexec helium-browser "))


class BrowserPolicyTest(unittest.TestCase):
    def test_chromium_theme_color_renders_the_template(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            theme, omarchy = root / "theme", root / "omarchy"
            theme.mkdir()
            (omarchy / "default" / "themed").mkdir(parents=True)
            (omarchy / "default" / "themed" / "chromium.theme.tpl").write_text("{{ background_rgb }}")
            (theme / "colors.toml").write_text('background = "#1C1917"\n')

            def run(command, **_kwargs):
                return captures.Result(0, "background\t#1C1917\nforeground\t#B4BDC3\n")

            self.assertEqual(captures.chromium_theme_color(theme, run, omarchy, root), "#1c1917")
            (theme / "chromium.theme").write_text("300,1,1")
            self.assertEqual(captures.chromium_theme_color(theme, run, omarchy, root),
                             captures.BROWSER_POLICY_DEFAULT_COLOR)

    def test_namespace_keeps_the_launched_pid(self):
        prefix = captures.policy_namespace(pathlib.Path("/tmp/p q"))
        self.assertEqual(prefix[:5], ["unshare", "--user", "--map-current-user", "--mount", "--keep-caps"])
        self.assertNotIn("--fork", prefix)
        self.assertIn("mount --bind '/tmp/p q' /etc/chromium/policies/managed", prefix[-1])
        self.assertTrue(prefix[-1].endswith('exec setpriv --inh-caps=-all --ambient-caps=-all "$0" "$@"'))

    def test_override_copies_other_policies_and_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            managed = root / "managed"
            managed.mkdir()
            (managed / "color.json").write_text('{"BrowserThemeColor": "#f0edec", "BrowserColorScheme": "device"}')
            (managed / "other.json").write_text('{"PasswordManagerEnabled": false}')

            def run(command, **_kwargs):
                return captures.Result(0, (root / "work" / "browser-policy" / "color.json").read_text())

            target = captures.browser_policy_override(root / "work", "#1c1917", "dark", run, managed / "color.json")
            if target is None:
                self.skipTest("unshare/setpriv not installed")
            self.assertEqual(json.loads((target / "color.json").read_text()),
                             {"BrowserThemeColor": "#1c1917", "BrowserColorScheme": "dark"})
            self.assertTrue((target / "other.json").exists())
            failing = captures.browser_policy_override(root / "work2", "#1c1917", "dark",
                                                       lambda *_a, **_k: captures.Result(1, ""), managed / "color.json")
            self.assertIsNone(failing)
            chrome = captures.browser_policy(managed / "color.json",
                                             {"BrowserThemeColor": "#1c1917", "BrowserColorScheme": "dark"})
            self.assertTrue(chrome["follows_mode"])
            self.assertEqual(chrome["machine_policy"], {"theme_color": "#f0edec", "color_scheme": "device"})
            self.assertFalse(captures.browser_policy(managed / "color.json")["follows_mode"])


FAKE_CAPTURE = """\
#!/bin/sh
# fake script/capture: record args, write one capture
echo "$@" > "$(dirname "$0")/../args.txt"
out=""
while [ $# -gt 0 ]; do [ "$1" = "--out" ] && out="$2"; shift; done
printf '{"surface": "notification", "mode": "dark", "scale": 1.5}' > "$out/notification-dark.json"
echo captured
"""


class CaptureRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls.directory.name)
        cls.root = root
        source = root / "design-system" / "tokens.toml"
        source.parent.mkdir()
        source.write_text('[color.light]\naccent = "#1853C7"\n')
        script = root / "script" / "capture"
        script.parent.mkdir()
        script.write_text(FAKE_CAPTURE)
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        cls.captures = root / "fake-captures"
        cls.captures.mkdir()
        (cls.captures / "osd-light.png").write_bytes(png_bytes(4, 2))
        (cls.captures / "osd-light.json").write_text(json.dumps({"surface": "osd", "mode": "light", "scale": 1.5}))
        (cls.captures / "stray.json").write_text("{not json")
        (root / "secret.png").write_bytes(b"secret")

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StudioHandler)
        cls.server.source = source
        cls.server.captures_dir = cls.captures
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.directory.cleanup()

    def call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data,
                                         headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.headers.get("Content-Type"), response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.headers.get("Content-Type"), error.read()

    def test_lists_captures_with_urls_and_plan(self):
        status, _, body = self.call("/api/captures")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual([item["surface"] for item in payload["captures"]], ["osd"])
        self.assertTrue(payload["captures"][0]["imageUrl"].startswith("/captures/osd-light.png?v="))
        self.assertIsNone(payload["captures"][0]["browserUrl"])
        self.assertIn("needs-fixture", {surface["status"] for surface in payload["surfaces"]})

    def test_serves_pngs_but_not_traversal(self):
        status, kind, body = self.call("/captures/osd-light.png")
        self.assertEqual((status, kind), (200, "image/png"))
        self.assertTrue(body.startswith(b"\x89PNG"))
        for path in ("/captures/..%2Fsecret.png", "/captures/../secret.png", "/captures/missing.png",
                     "/captures/osd-light.ppm", "/captures/.hidden.png"):
            self.assertEqual(self.call(path)[0], 404, path)

    def test_run_rejects_unsupported_surface(self):
        status, _, body = self.call("/api/captures/run", {"surface": "tooltip", "mode": "light"})
        self.assertEqual(status, 400)
        self.assertIn("hover", json.loads(body)["error"])

    def test_run_invokes_script_and_returns_updated_list(self):
        status, _, body = self.call("/api/captures/run", {"surface": "notification", "mode": "dark", "compare": False})
        payload = json.loads(body)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["result"]["output"], "captured")
        self.assertIn("notification", [item["surface"] for item in payload["captures"]])
        args = (self.root / "args.txt").read_text().split()
        self.assertEqual(args[:5], ["run", "--surface", "notification", "--mode", "dark"])
        self.assertNotIn("--compare-url", args)
        (self.captures / "notification-dark.json").unlink()



class ThemeFragmentTest(unittest.TestCase):
    """The generated hyprland_window.lua is part of a theme's chrome."""

    FRAGMENT = """\
-- Generated by script/design-system. Do not edit.
return {
\tgeneral = {
\t\tgaps_out = { top = 4, right = 12, bottom = 12, left = 12 },
\t\tcol = { inactive_border = "rgba(ffffff22)" },
\t},
\tdecoration = { shadow = { color = "rgba(10131a1f)", offset = { 0, 4 } } },
}
"""

    def test_fragment_values_merge_under_the_handwritten_file(self):
        with tempfile.TemporaryDirectory() as directory:
            theme = pathlib.Path(directory)
            (theme / "hyprland.lua").write_text(
                'hl.config(require("omarchy.current.theme.hyprland_window"))\n'
                'hl.config({ decoration = { shadow = { enabled = true } } })\n')
            self.assertNotIn("general", captures.parse_theme_lua(captures.theme_lua_text(theme))["config"])
            (theme / "hyprland_window.lua").write_text(self.FRAGMENT)
            config = captures.parse_theme_lua(captures.theme_lua_text(theme))["config"]
        flat = {captures.option_name(path): value for path, value in captures.flatten_config(config).items()}
        self.assertEqual(flat["general:gaps_out"], {"top": 4, "right": 12, "bottom": 12, "left": 12})
        self.assertEqual(flat["general:col.inactive_border"], "rgba(ffffff22)")
        self.assertEqual(flat["decoration:shadow:offset"], [0, 4])
        self.assertIs(flat["decoration:shadow:enabled"], True)
        self.assertEqual(captures.canonical(flat["general:gaps_out"]),
                         captures.canonical(captures.live_value({"css": "4 12 12 12"})))

    def test_bar_compares_against_the_full_width_desktop_bar(self):
        bar = next(surface for surface in captures.PLAN if surface.name == "bar")
        self.assertEqual(bar.specimen_selector("dark"), "#windows-view .desktop-specimen.dark-preview .menubar")
        self.assertEqual(bar.view, "windows")


if __name__ == "__main__":
    unittest.main()
