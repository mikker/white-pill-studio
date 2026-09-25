# Native captures

Native references are screenshots of the real Omarchy shell rendering this
repository's generated theme. They check what live HTML specimens cannot:
fontconfig resolution, Qt text rendering, compositor blur, shadows, and
geometry. They verify adapters. They never become token sources.

```text
captures/<surface>-<mode>.png           native capture (grim, monitor scale)       — ignored
captures/<surface>-<mode>.json          metadata                                   — tracked
captures/<surface>-<mode>.browser.png   live specimen from `compare` (Chromium)    — ignored
```

PNGs are ignored because their pixels depend on this machine's display,
fonts, and backdrop. The JSON is small, diffable, and records exactly what
produced each image, so it is tracked.

## Run

```sh
script/capture list                                        # the fixture plan
script/capture run                                         # every capturable surface, light and dark
script/capture run --surface notification --mode dark      # osd | notification | bar | windows | terminal | browser | all
script/capture run --surface windows terminal browser      # several surfaces; the window ones share one session
script/capture compare osd light --url http://127.0.0.1:4777   # needs a running studio
```

`run --compare-url URL` runs `compare` after each capture. `--page-url` sets the browser
fixture page (default `<compare-url or http://127.0.0.1:4777>/fixtures/browser-page.html`). The studio's
**Captures** tab has a **Capture** button per surface. It posts to
`POST /api/captures/run` with `{surface, mode}` and uses the header's
light/both/dark toggle as the mode. The studio runs the CLI as a subprocess
with a 120-second timeout. On timeout it sends SIGTERM rather than SIGKILL,
so the restore steps still run. Only one capture runs at a time.
`GET /api/captures` lists the metadata with image URLs, and `/captures/<file>`
serves only `*.png`/`*.json` files from this directory.

## Fixture plan

| Surface | Status | Fixture |
| --- | --- | --- |
| `osd` | supported, deterministic | `omarchy osd -i volume-medium -p 50 -d 0`: display-only (volume is never touched), persistent until `omarchy shell osd close` |
| `notification` | supported, deterministic | `omarchy notification send --app-name "White Pill Fixture" -t 30000 "White Pill capture fixture" "…"`, dismissed by summary |
| `bar` | supported, **live content** | grim of the `omarchy-bar` layer box from `hyprctl layers -j` |
| `windows` | supported, deterministic windows (live bar strip) | Headless output with ghostty tiled left and Helium tiled right; `grim -o` of the whole output. See [Window surfaces](#window-surfaces). |
| `terminal` | supported, deterministic | The ghostty client box from `hyprctl clients -j`, grown by `border_size`, captured from the same session |
| `browser` | supported, deterministic | The Helium client box, grown by `border_size`; chrome colored per mode (see Browser, step 4) |
| `tooltip` | needs-fixture | Bar tooltips open only on real pointer hover. There is no IPC to show one, and moving the user's pointer is not acceptable. |
| `audio-panel` | needs-fixture | `omarchy shell omarchy.audio open` shows live PipeWire devices and levels. A fixed state needs a mock graph or an upstream fixture mode. |
| `hunk` | manual | Hunk is not installed, and `~/.local/bin/hunk` is a mise shim that would change global mise config. Upstream's headless PTY-to-PNG path (tuistory + ghostty-opentui) is the planned route. |

The bar clock can be frozen with a literal `setBarWidget omarchy.clock format`.
That writes `~/.config/omarchy/shell.json`, which is a symlink into dotfiles,
so the harness does not do it. Clock, workspaces, tray, and indicators are
live, and the metadata says so (`deterministic: false`, `live: [...]`).

## Window surfaces

`windows`, `terminal`, and `browser` come from one fixture session per mode. When you
request any of them, the session produces every requested one.

1. **Headless output.** The harness registers an in-memory monitor rule
   (`hl.monitor`) for `WHITEPILL-CAPTURE`. The rule copies the focused monitor's
   physical size and scale (3840×2160 @ 1.5 → 2560×1440 logical) and places the
   output 20000 px to the right, so the pointer cannot reach it. It then runs
   `hyprctl output create headless WHITEPILL-CAPTURE`. The shell puts its
   wallpaper and bar on the new output. The fixtures use the output's own active
   workspace (its id is recorded in the metadata). A named workspace bound by a
   workspace rule would not be the one the output displays.
2. **Mode chrome.** The harness parses the literal `hl.config` tables and
   `o.window(".*", …)` rules from the mode's `hyprland.lua` and from the installed
   theme's copy. Each one is preceded by the generated `hyprland_window.lua` table
   it requires. It applies only the options where they differ (`theme_delta`)
   with `hyprctl eval hl.config(…)`. Both modes now share the generated chrome, so
   the delta is usually empty. An option whose live value differs from the
   installed theme is left alone (`theme_delta_skipped`) because later user
   config overrides it in either mode.
3. **Terminal.** `ghostty --config-default-files=false --config-file=<copy>` gets a
   copy of `~/.config/ghostty/config` with one change: the
   `current/theme/ghostty.conf` include points at this mode's `ghostty.conf`. The
   harness renders that file with the same template and `omarchy-theme-color` table
   that `omarchy-theme-set-templates` uses. For the installed theme it is
   byte-identical, and a test checks this. Everything else is the user's real
   config, including the personal `~/.dotfiles/ghostty/config` and, last,
   `~/.dotfiles/ghostty/linux.config` (the Linux point sizes). Effective values
   come from `ghostty +show-config` and are recorded under `ghostty.effective`:
   today Iosevka Nerd Font Mono then IBM Plex Mono at 12pt (the default, so not
   listed), padding 6 / 3,6, cell height +15%, cursor `#dd0455`, and
   `display-p3`. ghostty also applies the desktop's GTK text-scaling-factor
   (1.1818 here), which the metadata does not record. The only
   extra flags make the terminal a separate, disposable process (no single-instance,
   no cgroup, no shell integration, fixed title). It runs
   `captures/fixtures/terminal.sh`, which prints the studio's shared
   `web/fixtures/terminal-session.ans`. The script hides the cursor with DECTCEM
   (`ESC[?25l`), so no cursor cell differs between runs, then sleeps.
4. **Browser.** The launcher comes from the default browser's desktop file
   (`helium-browser`). It runs with a throwaway `--user-data-dir`, `--no-first-run`,
   `--password-store=basic`, `--window-size=1264,1387`, and a DevTools port. It loads
   `browser-page.html?scheme=<mode>`. The harness waits over DevTools until
   `readyState` is `complete` and the fonts have loaded. When the studio page is not
   reachable, it serves `captures/fixtures/browser-placeholder.html` from a local
   server, and `browser.page.source` says `placeholder`.

   **Chrome color per mode.** Chromium-family chrome color is managed policy
   (`/etc/chromium/policies/managed/color.json`, written by
   `omarchy-theme-set-browser` for the *installed* theme), which every profile obeys.
   The harness never touches it. Instead it copies the managed policy directory into
   the session's scratch directory, replaces `color.json` with this mode's values, and
   starts the browser in a private user and mount namespace where that copy is
   bind-mounted over the managed directory:
   `unshare --user --map-current-user --mount --keep-caps sh -c 'mount --bind … &&
   exec setpriv --inh-caps=-all --ambient-caps=-all helium-browser …'`.
   `unshare` does not fork and every step `exec`s, so the window's pid is the
   launched pid, which Hyprland's `workspace … silent` launch rule needs. (bwrap
   forks; its window mapped on the user's workspace instead, so it is not used.)
   `setpriv` drops the namespace capabilities before the browser starts.
   `BrowserThemeColor` is the mode's `chromium.theme` (Omarchy's template renders
   `{{ background_rgb }}`, the palette background; a theme's own file wins).
   Omarchy writes `BrowserColorScheme: "device"` and `omarchy-theme-set-gnome` sets
   the device scheme from the theme's mode, so the copy states that mode. The
   harness checks the namespace (`cat` of the mounted file) before launching.
   Metadata: `browser.chrome` has `follows_mode: true`, the colors used, `method`,
   and the untouched `machine_policy`. Without `unshare`/`setpriv` or unprivileged
   user namespaces, the browser runs as before and `follows_mode` is `false`.
5. **Layout and focus.** The terminal launches first, so dwindle (`force_split = 2`)
   tiles it left, split 50/50. Both windows open `silent`, so the user's focus never
   moves. Hyprland's focus is global, so the terminal is drawn active with
   per-window props instead of real focus: `inactive_border_color` = the active
   border, and `opacity_inactive` = its active opacity. Both windows also get the
   mode's opacity rule (dark theme `1 1`; Omarchy default `0.985 0.96` for
   terminals and `1.0 0.985` for browsers). The props applied are recorded in
   `window_props_set`, and the effective values read back with `hyprctl getprop`
   are in `windows.<role>.props`.
6. **Capture.** Hyprland sometimes software-renders the pointer onto the headless
   output at its monitor-local position. The harness sets
   `cursor:inactive_timeout = 0.1` for about a second so the pointer hides
   (`cursor:invisible` only applies on the next pointer move). It then runs
   `grim -o WHITEPILL-CAPTURE -s 1.5` for `windows` (3840×2160) and
   `grim -g <box> -s 1.5` for each window box (client `at`/`size` grown by
   `border_size`; 1266×1395 logical → 1899×2092 px, since grim truncates). The crop
   is recorded in `crop_pixels`.

The metadata adds the following fields to the common ones:

- `output`: name, headless, size, scale, workspace, reserved.
- `hyprland`: border, gaps, colors, rounding, shadow range, offset, and colors, all
  read live.
- `theme_delta` and `window_props_set`.
- `windows`: geometry, box, and props per window.
- `ghostty`: version, flags, effective config, `fc-match` of each font family,
  script, and the transcript's sha256.
- `browser`: name, launcher, DevTools version, package, flags, page, and chrome
  policy.
- `restore`: the exact steps performed.

### Window restore guarantees

`close()` runs in `finally`, including after an error or a signal. It is
idempotent and appends each step it performs to `restore`:

1. Put `cursor:inactive_timeout` back, and verify it.
2. Close each fixture window: SIGTERM its pid, wait, then SIGKILL any survivor in
   its process tree.
3. Kill anything whose command line still mentions the throwaway profile. Delete the
   profile, and stop the placeholder server.
4. Run `hyprctl output remove WHITEPILL-CAPTURE`, and verify the output is gone.
5. Re-apply the pre-capture value of every option in `theme_delta` (from
   `hyprctl getoption`), and verify each one with `getoption`.
6. Refocus the user's window only if a fixture window took focus. None does,
   because both open `silent`.

Leftovers:

- **Monitor rule.** The in-memory `hl.monitor` rule stays until the next Hyprland
  reload. It matches only the name `WHITEPILL-CAPTURE`.
- **`set` flag.** Options the harness re-applies (`cursor:inactive_timeout`,
  `theme_delta`) keep their values but report `"set": true` from then on.
- **Workspace indicator.** During the session the headless output's workspace
  briefly appears in the bar's workspace list.

### Window surface limits

- **Browser chrome color.** Follows the mode through a per-capture policy copy in a
  private mount namespace (step 4). Where user namespaces are unavailable the chrome
  keeps the installed theme's color (`browser.chrome.follows_mode: false`).
- **Terminal shadow.** Hyprland has no per-window shadow color, so the
  active-looking terminal casts `shadow:color_inactive`. The generated chrome
  makes both shadow colors `shadow.floating`, so this no longer shows. Taking
  real focus would steal the user's keyboard focus.
- **Live content.** The bar strip at the top of `windows` is live (clock,
  workspaces), and the wallpaper is the user's current background. The
  `terminal` and `browser` crops contain neither.
- **Terminal colors in dark mode.** `theme = light:…,dark:…` in the personal ghostty
  config follows the system color scheme. Explicit Omarchy colors override it, so
  only keys that neither file sets could differ.

`compare` for window surfaces screenshots
`#windows-view .desktop-specimen.<mode>-preview` for `windows`, or its
`.hypr-window.is-terminal` / `.is-browser` tile (border included) for the crops.
The specimen classes sit on the `.desktop-specimen` element itself. The standalone
terminal and browser stages in the Windows view have a different aspect, so the
harness uses the tiles inside the desktop. The screenshot has no margin, in a
2400×1600 viewport, at the device scale that makes the specimen as wide as the
native PNG (3.0 here, recorded as `comparison.device_scale`).

`script/align-check` measures the terminal and browser more closely: it resamples
each native crop to logical px (box filter, honouring the half-pixel crop offset in
the metadata), screenshots the standalone 1:1 stages, and prints per-region RMSE,
landmark edges (text grid, omnibox, content top, icons), ANSI swatch colors, and
ghostty's effective config against `recipe.terminal`. Pass `--url` to use a running
studio and `--captures DIR` for another capture directory. The specimen's omnibox
always shows `127.0.0.1:4777` (the default page URL), so capture with the page on
4777 (the default, or `--page-url`) for an exact URL match.

`script/align-check --surface osd notification` does the same for the shell cards.
It screenshots the Shell view's specimen with the 24px capture margin at the native
device scale, resamples both images to logical px, finds each card by its inner
keyline (scanning outward from the padding, so the backdrop does not matter), and
reports the card size, the OSD track, fill, icon, and readout boxes, and the
notification's title and body lines relative to that keyline.

`compare` for `bar` screenshots the full-width bar of the same desktop specimen
(`#windows-view .desktop-specimen.<mode>-preview .menubar`, 2560 logical px). It
uses the same rule, so the specimen and the grim of the layer box share one
pixel grid. The user's bar is transparent (`shell.json` `bar.transparent`), so
its pixels are mostly wallpaper and its text color follows the wallpaper. Treat
the bar RMSE as backdrop-dominated.

## How a capture works (shell surfaces)

1. **Payload.** For each mode, the harness reads the generated `colors.toml`
   and `shell.<section>.toml` fragments from the theme directory the generator
   writes (manifest target, else
   `$WHITE_PILL_DOTFILES/omarchy-themes/mekanikos{,-light}`). It merges them
   the way `omarchy-theme-set-templates` does:
   - it renders `shell.toml.tpl` with colors resolved by `omarchy-theme-color --all`;
   - it overlays each fragment's section in file-name order.

   The Python merge is byte-identical to the installed theme's `shell.toml`
   when given the same inputs.
2. **Apply to the running shell only.** `omarchy shell shell applyTheme
   <b64 colors> <b64 shell>` reloads colors and style in memory. It does not
   change the theme state on disk, and `omarchy theme current` is untouched.
3. **Show and locate.** The OSD and notification layers are fullscreen
   overlays, so `hyprctl layers -j` only confirms that the layer is mapped.
   To find the card, the harness keeps the fixture on screen and grabs the
   search region twice: once as rendered, and once with a probe payload that
   paints that section's background (`[popups]` or `[notifications]`) opaque
   black (light) or white (dark) for about 0.35 s. Only the card differs
   between the two frames, whatever live windows sit behind it, so the diff
   gives its exact box. The harness grows that box by 24 logical px for the
   shadow and restores the real payload.
4. **Capture.** `grim -g "<box>" -s <monitor scale>` writes the PNG at native
   pixels, including compositor blur. Hardware cursors are forced on
   (`cursor:no_hardware_cursors = 0`) so a software cursor is not baked in.
5. **Close** the fixture and wait for the layer to unmap.

## What is deterministic

- The fixture content, the generated payload (the metadata records its
  sha256), monitor scale, fonts, and package versions.
- The OSD and notification box: repeated runs give the same size.

What is not deterministic:

- **The backdrop.** Popups are translucent and blur whatever window is behind
  them. The harness never switches workspaces or moves windows. It records the
  window classes under the box in `backdrop`. Dark-mode references therefore
  sit over the user's current, possibly light, desktop.
- The bar's live content (see above).
- Animation timing: the harness waits a fixed 0.7 s after the layer maps.

## Restore guarantees

Everything below runs in `finally` blocks, including on errors, SIGTERM, and
SIGHUP:

- **Theme:** the harness re-applies the payload the shell loaded from
  `~/.local/state/omarchy/current/theme/{colors,shell}.toml`.
- **Cursor:** `cursor:no_hardware_cursors` goes back to its prior value.
- **OSD:** `omarchy shell osd close`.
- **Notification:**
  1. Before sending, the harness refuses if Do Not Disturb is on or another
     toast is showing, then backs up `notifications/history` and `images`.
  2. After dismissing, it waits for the shell's archive-and-trim job. The
     shell keeps only the newest 10 entries, so archiving the fixture evicts
     the oldest real one.
  3. It deletes the fixture's history entry and its images, and copies back
     any trimmed entries.
  4. It checks the restore and repeats it up to three times. If entries are
     still missing, it keeps the backup and reports its path.

  It never runs `notifications clear`.

Metadata fields: `date`, `surface`, `mode`, `box` (logical), `scale`,
`pixels`, `monitor`, `method`, `located_by`, `deterministic`, `live`,
`backdrop`, `versions` (quickshell, qt6-base, hyprland, grim), `dotfiles`
(path, commit, dirty), `studio` (commit, or `uncommitted` plus the sha256 of
`tokens.toml`), `theme` (user theme, applied directory, payload sha256s),
`font` (the `fc-match` result for `type.bar`, and whether it fell back), and,
after `compare`, `comparison` (browser clip and RMSE).

`compare` screenshots the matching live specimen in headless Chromium at the
native device scale, with the same margin. It resizes that screenshot to the
native pixel size and prints ImageMagick's RMSE. The backdrops differ, so
treat the number as a trend, not a pass/fail gate.
