# White Pill Studio

A local style studio for tuning Mekanikos from one canonical token source and
generating its downstream application themes.

White Pill is intentionally local and dependency-free. The server uses the
Python standard library; the interface is plain HTML, CSS, and JavaScript.

## Run

```sh
cd ~/dev/white-pill-studio
./bin/white-pill-studio
```

Open <http://127.0.0.1:4777>. The server binds to localhost only.

The default canonical source is `design-system/tokens.toml`. Generated
consumer files are written to every repository listed in
`design-system/consumers.toml` (by default `~/.dotfiles`); override the
dotfiles checkout with:

```sh
WHITE_PILL_DOTFILES=~/path/to/dotfiles ./bin/white-pill-studio
```

### Consumers

`design-system/consumers.toml` declares each consumer repository:

```toml
[[consumer]]
name = "dotfiles"                  # display prefix: dotfiles:<path>
root = "~/.dotfiles"               # ~ and $VAR expand; relative to this repo
env = "WHITE_PILL_DOTFILES"        # optional variable that overrides root
adapters = ["omarchy-plugin", "omarchy-theme", "hunk"]
```

This repository's own `design-system/tokens.css` is always generated as the
implicit `studio` consumer. `WHITE_PILL_CONSUMERS=path/to/other.toml` swaps
the whole file. `script/design-system consumers` prints the resolved roots,
whether they exist, and each repository's git HEAD and dirty state.

## Workflow

- Token edits update every browser specimen immediately.
- **Save & generate** first shows a change report — changed tokens (raw and
  resolved, with swatches), then every generated field that would change,
  grouped by consumer repository and file — and only writes after you
  confirm. It then validates the edit, writes the canonical TOML atomically,
  and regenerates all configured adapters. Validation errors block the save;
  warnings do not.
- **Apply** shows the same report when there are unsaved edits, then runs
  `omarchy theme refresh`, which re-stages the current theme from its
  templates and reloads the shell, Hyprland, terminals, and browser policy.
  Omarchy reads a staged copy of the theme, so a plain shell restart would
  not pick up regenerated fragments.
- Generated files belong to their consumer repositories and must not be
  edited by hand.

The studio includes shell, Hunk, window, and foundation specimens, plus a
**Captures** tab that pairs live specimens with native Omarchy captures.

### Window specimens

The **Windows** tab renders the 2560×1440 logical desktop at half size — the
bar and two tiled Hyprland windows, a ghostty terminal (active) and a Helium
browser (inactive) — and then each window at 1:1 logical px, light and dark.
Border, gaps, rounding, shadow, opacity, terminal colors, and browser chrome
come from `recipe.window`, `recipe.terminal`, and `recipe.browser`. Content is
fixed so native captures can match it exactly:

- `web/fixtures/terminal-session.ans` — the terminal screen as ANSI text;
  `cat` it in ghostty for the native side.
- `web/fixtures/browser-page.html` — the page both sides load
  (<http://127.0.0.1:4777/fixtures/browser-page.html>); `?scheme=dark` forces
  dark mode for the studio's dark specimen.

### Native captures

```sh
script/capture list                                   # fixture plan and why some surfaces are not capturable yet
script/capture run --surface osd --mode both          # osd | notification | bar | all; light | dark | both
script/capture compare notification dark --url http://127.0.0.1:4777   # browser specimen + RMSE
```

`run` applies this repository's generated theme to the *running* shell only
(`omarchy shell shell applyTheme`), shows one fixture at a time, captures it
with grim at the monitor's scale, and always restores the user's theme
payload, cursor setting, and notification history. It never changes the
persistent Omarchy theme. The Captures tab's **Capture** button calls
`POST /api/captures/run`; `GET /api/captures` lists metadata and image URLs.
Details, guarantees, and limits: [`captures/README.md`](captures/README.md).

## Commands

```sh
script/design-system generate   # validate, then regenerate studio CSS and every consumer's adapters
script/design-system check      # validate and detect output drift, per consumer repository
script/design-system validate   # list color, opacity, dimension, contrast, key, and reference issues
script/design-system manifest   # print every consumer, generated target, and field as JSON
script/design-system consumers  # print resolved consumer roots and their git state (--json)
script/design-system report --set color.light.accent=#286486   # what a change would regenerate
script/design-system report changes.json --json                # same, from {path: value} JSON
script/design-system release 2  # validate, check drift, bump [meta] version, regenerate, print the tag
script/design-system release 2 --tag   # once the bump is committed: git tag design-system/v2
script/design-system export site/      # static read-only studio (web/, api/*.json, captures) for GitHub Pages
script/visual-baseline check    # compare live specimens with tests/baselines/*.png
script/visual-baseline update   # re-record the baselines after an intended visual change
script/browser-smoke --url http://127.0.0.1:4777   # drive a running studio headlessly (never saves)
script/capture run --surface all --mode both        # native Omarchy references (see captures/README.md)
python -m unittest discover -s tests
make check                      # drift check, unit tests, and visual check when Chromium exists
```

`report` exits 1 when the draft has validation errors; the studio serves the
same report as `POST /api/report` with `{"changes": {...}}`.

Design-system revisions are tagged in this repository as
`design-system/v<version>`, independently from the consumer repositories:
`release <version>` bumps and regenerates without committing; commit the
bump, then `release <version> --tag` refuses unless the tree is clean and the
version is already committed. Regenerated consumer files are committed in
their own repositories.

### Visual baselines

`tests/baselines/` holds committed PNGs of each live specimen — shell desktop,
Hunk window, foundation swatches, and the Windows view's tiled desktop,
terminal, and browser, light and dark — captured in headless
Chromium at a 1600×1000 viewport, device scale factor 1, with animations and
transitions disabled. `script/visual-baseline` starts a private read-only
studio on a free port (or uses `--url`), recaptures, and diffs pixels with a
pure-stdlib PNG decoder; failures write the capture and a red-on-gray diff
image to `$TMPDIR/white-pill-visual` (`--diff-dir`). `--tolerance` sets the
fraction of pixels allowed to differ and `--threshold` the per-channel delta
ignored (both default 0). `meta.json` records the Chromium build.

Captures depend on the installed fonts (Inter, Iosevka Nerd Font Mono), the
Chromium build, and font rendering, so baselines are only meaningful on the
machine that recorded them. Token edits change the specimens by design:
after an intended change, run `update` and review the PNGs.

## Repository map

```text
design-system/   canonical tokens and design-system notes
references/      selected and historical visual explorations
script/          generator, reports, releases, visual baselines, browser smoke
tokens_model.py  token references, schema, and validation
adapters.py      consumer configuration, adapter tables, and manifest
report.py        change reports for a token revision
cdp.py           tiny headless-Chromium DevTools client
visual.py        stdlib PNG decode/encode and pixel diff
web/             interactive studio
web/fixtures/    fixed terminal and browser content for window specimens
tests/           model, adapter, consumer, report, visual, server, and source-editing tests
tests/baselines/ committed live-specimen PNG baselines
captures.py      native capture harness (fixtures, restore, metadata, studio routes)
captures/        native reference PNGs (ignored) and their JSON metadata
```

The architectural contract and delivery phases live in [`PLAN.md`](PLAN.md).

