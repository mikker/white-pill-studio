# Mekanikos design system

`tokens.toml` is the hand-authored source of truth. HTML/CSS is the visual publication and review format; generated app files remain native to each consumer.

## Direction

**Frosted Utility** uses luminous translucent surfaces, a double edge, and a broad low-opacity shadow. Every translucent recipe has a solid fallback because blur, compositing, and alpha differ across renderers.

Light and dark are equal modes. They share one semantic contract and geometry; only compositing values and role colors change.

## Core hierarchy

The intentionally small hierarchy prevents each app from inventing near-duplicate values:

- **Accent:** `#1853C7`, the blue used by the active Hyprland window border. Reserve it for active, selected, focused, and actionable state.
- **Text colors:** primary, secondary, muted.
- **Text sizes:** small `12px`, body `14px`, title `16px`.
- **Families:** Inter for UI language; weight is selected separately rather
  than encoded in the family name. Iosevka Nerd Font Mono is reserved for
  code, metadata, and icon glyphs.
- **Bar type:** the shell bar uses the `Inter Medium Tabular` fontconfig alias
  at the body size (14px), which selects the Inter Medium face. The alias is
  necessary because Omarchy's plugin API currently exposes only a family
  string, not numeric weight or OpenType features. The alias lives in the
  user's `fonts.conf` (dotfiles `omarchy/fontconfig/fonts.conf`) as a
  scan-time subfamily name, because Qt only resolves family names that fonts
  carry. It also requests `tnum`, but Qt ignores fontconfig features, so the
  bar's numerals are proportional (see the alignment log).
- **Density:** shell geometry does not scale with the 14px type root. Panels
  use 14px popup padding, 10px horizontal row insets, 10px section gaps, 6px
  row gaps, and 28px controls. OSD-specific density should not change this
  shared popup geometry.
- **OSD density:** volume and playback feedback uses independent `20px`
  horizontal and `10px` vertical padding; it does not inherit PopupCard's
  symmetric padding.
- **Dark glass edge:** dark popup and OSD surfaces use white at `0.22` alpha
  with a `0.08` inner keyline: lighter and more opaque than the background,
  but still quieter than the light-theme edge.
- **Controls:** macOS-like accent emphasis—solid blue primary actions, switches,
  checks, and slider progress; white foregrounds on solid accent fills; softer
  blue tint for persistent list selection.

Popup typography composes those foundations consistently:

- **Title:** title size, primary text, semibold, slightly tightened tracking.
- **Context subtitle:** small size, secondary text, medium weight, no tracking.
- **Section header:** small sentence-case medium text in the secondary color.

Use the `Inter` family at every weight and request weight independently. Do
not use `Inter SemiBold` as a family and then apply bold; Qt synthesizes another
weight pass and the result looks cramped and uneven. The bar's fontconfig alias
is the sole exception because its API cannot express weight or font features.

The system stays intentionally small:

- foundations: light/dark color, typography, spacing, radius
- semantics: canvas, raised/alternate surfaces, text, accent, status
- effects: subtle/glass borders, low/floating shadows, blur, motion
- recipes: compose foundations for a specific surface; do not invent component-local colors

## Token references

Any scalar string may reference another token by dotted path:

```toml
[color.light]
accent = "@palette.light.accent"

[control.light]
focus-ring = "@color.light.accent"
```

References resolve transitively. Unknown targets, tables, cycles, and type
mismatches are validation errors. Semantic roles link to foundations only
where the value is genuinely shared; a role that should diverge stays literal.
Saving a literal over a reference in the studio unlinks it; saving `"@path"`
links it again. Foundations and semantic roles may not reference recipes.

## Recipes

`[recipe.<name>]` tables describe UI intent — `bar`, `window`, `panel`,
`tooltip`, `notification`, `osd`, `overlay` (launcher and menu), `controls`,
`code-review`, `terminal`, and `browser` — by referencing semantic roles and
foundations. Literal
recipe values are recipe decisions, such as the bar's 29px height or the
controls' resting fill.

Recipes are rendered once per mode. `{mode}` inside a recipe reference becomes
`light` or `dark`, and `[recipe.<name>.<mode>]` overrides individual fields for
one mode; the panel uses this to pick the `glass` or `glass-dark` edge.
Recipes may reference other recipes (`"@recipe.panel.border"`), which honours
the target's mode overrides.

### Window, terminal, and browser

These recipes describe application windows as the desktop draws them. They
drive the studio's **Windows** specimens and are provenance-visible.
`recipe.window` is generated (the `hyprland` family, below); the terminal and
browser recipes are not yet.

- **`[window]` foundation and `recipe.window`** drive each theme's generated
  `hyprland_window.lua`: 4px border inside the window's tile
  (`border_part_of_window`, handwritten), 2px inner gaps, 12px outer gaps but
  4px on top (`gaps-outer-top`, matching the user's `looknfeel.lua`),
  `rounding` → `radius.surface` (14, circular corners at `rounding_power` 2),
  and `shadow.floating` (color, alpha 0.12, 18px range, (0, 4) offset) in
  both focus states. The active border is the accent; the inactive border is
  the semantic `color.<mode>.window-border-inactive` (white at `0.133`) in
  both modes. `opacity-active` and `opacity-inactive` record Omarchy's
  default-opacity rule (0.985/0.96); `[recipe.window.dark]` records the dark
  theme's opaque-window rule (1/1). Window rules stay handwritten.
- **`recipe.terminal`** records ghostty as it renders. Colors come from
  Omarchy's `ghostty.conf` template, which renders `colors.toml` (the
  palette): background, foreground, and ANSI 0–15 (0/7/8/15 are
  background/foreground/muted/bright_foreground), selection, and selection
  foreground (`bright_foreground`). Font size (12pt) and padding (6 / 3,6pt)
  come from `~/.dotfiles/ghostty/linux.config`, which owns the Linux point
  values. Cell height (+15%) and the `#DD0455` cursor come from the shared
  `~/.dotfiles/ghostty/config`. `font-scale` is the desktop's text-scaling
  factor, which ghostty applies. `cell-width`/`cell-height` (9.5 × 27.5) and
  the padding offsets are the grid measured in the native capture, which the
  specimen reproduces (see the alignment log).
- **`recipe.browser`** models Helium (Chromium). `seed` is the palette
  background, which Omarchy's `chromium.theme` template publishes as the
  `BrowserThemeColor` policy. Chromium derives every chrome tone from that
  seed, so `[recipe.browser.light]` and `[recipe.browser.dark]` hold the
  measured tones (frame, omnibox, new-tab button, text, icons) rather than
  palette references. The geometry (34px tab strip, 31px toolbar, 28px
  omnibox and new-tab button with 8px radii, a 1px transparent separator,
  and the page inset 4px with 8px corners) and the 15px/14px Inter UI text
  are Helium's at 1:1 logical px. Opacity records Omarchy's browser rule
  (1.0/0.985).

The specimens use fixed fixtures in `../web/fixtures/`: `terminal-session.ans`
(ANSI text on a 100×28 cell grid; the native capture `cat`s the same bytes) and
`browser-page.html` (self-contained; `?scheme=light|dark` forces a mode).

## Adapters

`../adapters.py` maps recipe fields onto consumer files as plain tables of
`(consumer key, source)` rows. A source is a recipe field, a direct token for
foundation pass-through (fonts, spacing, the palette), or a literal for
consumer configuration that is not a design decision, such as Hunk's
`theme = "custom"` header. Adapters never compute values.

`script/design-system manifest` prints the configured consumers and every
generated target as JSON: absolute path, consumer repository name and root,
display label (`<consumer>:<relpath>`), adapter family, mode, and each field's
section, key, recipe field, semantic token, resolution chain, and value. The
studio serves the same manifest and derives token provenance from it:
foundation → semantic role → recipe → consumer field.

## Consumers and releases

`consumers.toml` lists the repositories that receive adapters and which
adapter families (`omarchy-theme`, `omarchy-plugin`, `hyprland`, `hunk`) each
one gets.
`generate` writes to all of them and `check` verifies all of them, reporting
per repository; a missing root is an error naming the consumer.

Before a revision is applied, `script/design-system report` (and the studio's
review dialog) lists the changed tokens and every consumer field that would
change, file by file. `script/design-system release <version>` bumps
`[meta] version` after validation and a clean drift check; the revision is
tagged `design-system/v<version>` in this repository, separately from the
consumer repositories' own history.

## Validation

`script/design-system validate` reports:

- `color` — color roles must be `#RRGGBB`;
- `opacity` — `alpha` and `*-alpha` values must be numbers from 0 to 1;
- `dimension` — spacing, radius, blur, shadow, type sizes, and motion
  durations must be numbers; only shadow offsets may be negative;
- `contrast` — primary text on canvas and on the raised surface composited
  over canvas, per mode, warns below 4.5:1 and fails below 3:1; control
  foreground on primary background warns below 4.5:1 (WCAG 2.x luminance);
- `unknown-key` — keys outside the declarative schema in `../tokens_model.py`
  (warning; `palette.*` is a free color map);
- `reference` — unknown targets, cycles, and type mismatches.

`check` and `generate` refuse to run with validation errors, and the studio
refuses to save them. Warnings never block.

## Wide gamut

The sRGB hex values remain canonical fallbacks for native Linux UI, terminals,
TOML themes, and other consumers without explicit color-space support. CSS also
publishes a richer Display P3 accent and activates it only inside both
`@supports (color: color(display-p3 ...))` and `@media (color-gamut: p3)`.

That distinction matters: parsing Display P3 syntax does not prove the complete
display pipeline is wide gamut. The compositor, output profile, application,
and monitor must all participate. Unsupported consumers continue to receive
`#1853C7` without conversion or clipping surprises.

## Files

- `tokens.toml` — canonical values and recipes
- `tokens.css` — generated CSS custom properties
- `../references/mekanikos-foundations-B.reference.html` — visual reference
- `../tokens_model.py` — references, schema, validation, and contrast
- `consumers.toml` — consumer repositories and the adapter families they receive
- `../adapters.py` — consumer configuration, adapter tables, manifest, and provenance
- `../report.py` — change reports for a token revision
- `../align.py`, `../script/align-check` — measured alignment of the terminal,
  browser, OSD, and notification specimens against their native captures
  (RMSE and landmarks)
- `../script/design-system` — generator, drift check, validator, manifest,
  consumers, change report, and release

Run:

```sh
script/design-system generate
script/design-system check
script/design-system validate
script/design-system manifest
script/design-system consumers
script/design-system report --set color.light.accent=#286486
script/design-system release 2
```

The generator writes consumer adapters to every repository in
`consumers.toml` (`~/.dotfiles` by default). Set `WHITE_PILL_DOTFILES` to
target another dotfiles checkout. The dotfiles `script/check`
runs this repository's drift check too.

## Mapping policy

Generate values, not behavior. QML structure, Hyprland rules, application bindings, and accessibility behavior remain handwritten.

Current generated consumers:

- Mekanikos light/dark foundational palettes
- Omarchy popup, notification, tooltip, and OSD (`[osd]`, read by `mikker.osd`)
  theme fragments for both modes, plus the popup title and subtitle weights
  and title tracking in `[font]`
- Omarchy control-state fragments for hover, focus, selection, and toggles
- Complete Omarchy bar, menu, launcher, and Hyprland-border sections for both
  modes, so section replacement cannot silently drop text or surface colors
- Hyprland window chrome (`hyprland` family): `omarchy-themes/<slug>/hyprland_window.lua`
  returns a table of border size and colors, gaps, rounding, shadow, group
  border colors, and borders-plus-plus size and color. The theme's
  handwritten `hyprland.lua` applies it first with
  `hl.config(require("omarchy.current.theme.hyprland_window"))`, then sets
  its own blur, shadow rendering, layer rules, and window rules. Omarchy
  copies the fragment with the theme (`omarchy theme set`/`refresh`).
- Hunk light/dark semantic and syntax themes
- CSS custom properties for publication and browser styling

Next adapters should be added one family at a time: Pi, remaining terminal/editor syntax, browser sprinkles, then macOS shell chrome. Their semantic role mapping should be reviewed before generation replaces existing files.

## Omarchy constraint

**Font families reach only user-owned plugins.** The upstream shell binds
every `qs.Ui` component to the fontconfig `monospace` alias that
`omarchy font set` writes, and reads only the integer sizes from a theme's
`[font]` section (`Commons/Style.qml`: "the family stays system-wide"). So
`type.ui`, `type.ui-strong`, `type.bar`, and `type.mono` change the
mikker.bar, mikker.osd, and mikker.notifications plugins, which read
`font.ui-family`, `font.bar-family`, and `font.icon-family` through
`DesignTokens.js`, but not the launcher, menu, or first-party panels. GTK
apps and the browser chrome follow the GNOME `font-name` setting instead.

**Family names must be installed names.** Qt asks fontconfig for the family
by name; a name no font carries (for example `Iosevka` when only
`Iosevka Nerd Font`, `Iosevka Nerd Font Mono`, and `Iosevka Nerd Font Propo`
are installed) is silently substituted, and the surface renders in the
desktop font. The validator checks every family token with `fc-match` and
reports a `font` error naming the font that would actually be drawn and the
installed families that match.

User-owned OSD, notifications, bar tooltip, and tray popup code can consume the effect tokens now. First-party network/audio/Bluetooth/power panels use package-owned `Ui/KeyboardPanel.qml`; Omarchy currently has no supported user override for that shared component. A small upstream shadow-token consumer is preferable to cloning every panel and freezing upstream code.

## Native references

Live specimens are the tuning loop; native captures (`../captures/`) verify
what Omarchy actually renders: fontconfig resolution, Qt text rendering,
compositor blur, shadows, and geometry. They never become token sources.
Each capture's metadata records the generated payload hashes and the
`fc-match` result for `type.bar`, so a missing fontconfig alias shows up as a
`fallback` rather than as a subtle rendering difference. See
`../captures/README.md` for the fixtures, what is deterministic, and what still
needs a fixture.

## Alignment log

Decisions from aligning the proposed look (tokens and specimens) with the
actual desktop (native captures). The actual moves first; the proposed moves
only where the actual cannot come closer. Each entry: what differed, what was
tried, which side moved, and why.

### Window chrome and shell bar

- **Window chrome source.** Differed: the theme's handwritten `hyprland.lua`
  duplicated border, gap, rounding, shadow, group, and borders-plus-plus
  values that the tokens only mirrored. Tried: a generated per-theme Lua table
  (`hyprland_window.lua`) applied with one `hl.config(require(...))` line.
  Moved: actual. Hyprland now reads those values from `recipe.window`, and the
  handwritten file keeps only blur, shadow rendering, and rules.
- **Top outer gap.** Differed: the specimen used 12px on every side, so windows
  started at y=41; native windows start at y=33 because the user's
  `looknfeel.lua` sets `gaps_out` to 4 12 12 12. Tried: nothing on the actual
  side, because that override is a deliberate, committed user choice. Moved:
  proposed (`window.gaps-outer-top = 4`). The fragment also emits 4 12 12 12,
  so the theme agrees with the override instead of being silently replaced.
- **Window shadow alpha.** Differed: Hyprland used 0.094 active and 0.051
  inactive; `shadow.floating` is 0.12. Tried: the fragment sets both
  `shadow:color` and `shadow:color_inactive` to `shadow.floating` (rgba
  10131a1f), then re-captured. The shadow stays a soft edge over the
  wallpaper. Moved: actual. One shadow for every raised surface keeps the
  system small, the focus ring carries focus, and it removes a mismatch that
  Hyprland cannot express in captures: there is no per-window shadow color,
  so an emulated-focus window always cast `color_inactive`.
- **Dark inactive border.** Differed: the dark `hyprland.lua` used
  rgba(55555588); `color.dark.window-border-inactive` (the documented semantic
  role) is white at 0.133. Tried: generating the token into the dark fragment.
  Over the dark canvas the two composite within a few levels of each other.
  Moved: actual.
- **Dark window opacity.** Differed: the specimen applied 0.985/0.96 in dark;
  the dark theme's rule `o.window(".*", { opacity = "1 1" })` makes windows
  opaque. Tried: nothing on the actual side, because window rules are
  behavior and stay handwritten. Moved: proposed (`[recipe.window.dark]`
  opacity 1/1).
- **Border placement and radius.** Differed: nothing. Tried: comparing edge
  and corner profiles of the native and specimen captures at 1.5x. The
  4px border sits inside each 1266×1395 tile with an 18px outer and 14px inner
  radius in both, and edges align within 0.67 logical px. Moved: neither.
- **Bar font.** Differed: the native bar fell back to Liberation Sans because
  `Inter Medium Tabular` did not exist. Tried: a fontconfig pattern alias
  (fc-match resolved it, but Qt still drew Inter Regular), then a scan-time
  subfamily name on the Inter Medium face (Qt resolves it to Inter Medium).
  Moved: actual (user `fonts.conf`, `fc-cache -f`, theme refreshed).
- **Bar tabular numerals.** Differed: the specimen set `tnum`, but Qt ignores
  fontconfig `fontfeatures`. The same "1111" measures 23.19px with and without
  the alias; tabular would be 36.1px. The upstream `WidgetButton` exposes no
  font features. Tried: fontconfig `tnum`, which is kept for fontconfig
  clients. Moved: proposed. The specimen drops `font-feature-settings`.
- **Bar text color.** Differed: the light-mode native bar showed near-white
  text although `[bar] text` is #2C363C. Tried: tracing it. The user's
  `shell.json` sets `bar.transparent = true` (a committed choice), so
  mikker.bar runs upstream `omarchy-bar-text-color`. That script picks the
  theme text or the theme background (#F0EDEC), whichever contrasts more with
  the wallpaper under the bar, and the current wallpaper is dark. It is not a
  plugin bug, a stale theme, or a generated-fragment problem. Moved: proposed.
  `recipe.bar.transparent` and `transparent-text` record the behavior, and the
  specimen bar paints no surface and applies the same contrast rule against
  what is behind it. The chosen color therefore depends on the wallpaper, so
  light bar comparisons stay backdrop-dominated.
- **Bar geometry.** Differed: the specimen used 11px type, 13px gaps, and 11px
  insets, with a ◆ glyph and accent numerals. Native uses 14px (`type.size-body`)
  labels in 7.5px-margin buttons, an 8.5px edge inset, the omarchy menu glyph,
  20px workspace buttons 1px apart, dimmed empty workspaces, and an accent
  rounded square for the focused one. Tried: nothing on the actual side,
  because it is the plugin's layout. Moved: proposed (`recipe.bar.font-size`
  and the specimen CSS). Left-cluster glyphs and the right inset now match
  within 0.67 logical px.

### Terminal and browser

Measured with `script/align-check` (native crop resampled to logical px
against the standalone 1:1 specimen; deltas are native − specimen).

- **Terminal colors.** Differed: nothing. Tried: comparing ghostty's effective
  config (`+show-config`, in the capture metadata) with `recipe.terminal`, and
  sampling the 16 ANSI swatches in the capture. Omarchy's template maps every
  slot the way the recipe does (0/7/8/15 = background/foreground/muted/
  bright_foreground; selection and selection foreground match), and the dark
  swatches equal the palette exactly (light: within 4 levels, from the 0.985
  window opacity over the wallpaper). Moved: neither. No ghostty adapter: the
  colors already reach ghostty through `colors.toml`, and nothing else
  token-driven is missing.
- **Terminal cursor.** Differed: the recipe said `bright_foreground` (the
  template); the actual is `#DD0455` with white text. Tried: nothing on the
  actual side. The user's `~/.dotfiles/ghostty/config` sets it deliberately,
  and this README says nothing about the cursor color. Moved: proposed
  (`cursor`, `cursor-text` literals).
- **Terminal type and grid.** Differed: the recipe had 12px type, a 1.2 line
  height, and 14px padding, a guess from `~/.config/ghostty/config`. The
  first capture showed 16pt Iosevka at 21.33px with a 10.5 × 31 grid. The
  cause: `~/.dotfiles/ghostty/config` is shared with macOS, where ghostty
  renders 1pt = 1px. Linux converts points at 96 dpi (1pt = 1.333px), so the
  same 16pt rendered a third larger here. Tried: fixing the actual side (rule
  1). `~/.dotfiles/ghostty/linux.config`, included last by `ghostty/linux.sh`,
  now owns the Linux point values: the shared values × 0.75 (12pt, padding
  6 / 3,6). Moved: actual (the user's config), then proposed to match the new
  capture. `font-size = 12` (points) and `cell-height-adjust = 0.15` record
  the config. ghostty also applies the desktop's GTK `text-scaling-factor`
  (1.1818 on this machine, set outside ghostty and this repository), recorded
  as `font-scale`. So 12pt renders at 16 × 1.1818 = 18.9px, not 16px; a 16px
  specimen misses by 1–2px per line (text RMSE 24.6). `cell-width`/
  `cell-height` record the grid ghostty draws, 9.5 × 27.5 logical px:
  ghostty renders at buffer scale 2 and rounds cells to whole device px
  (19 × 55). The specimen sets the line box to the cell height and
  letter-spacing to cell width − 1ch. Result: both edges of two full lines,
  the text origin, and the swatch row match to 0px; line tops are within
  1px. Text RMSE against the new capture is 39.6 → 9.8 (light) and
  35.0 → 8.2 (dark).
- **Terminal padding.** Differed: ghostty's padding is in points, scaled like
  the font, and rounded down at buffer scale 2. `window-padding-x = 6`
  puts text 9px in (6 × 1.333 × 1.1818 = 9.45 → 18 device px), and
  `window-padding-y = 3` puts it 4.5px down (4.73 → 9 device px). Tried: the
  unrounded 8 / 4px (text 1px left of and above native), then the rounded
  values. Moved: proposed (`padding-x = 9`, `padding-top = 4.5`; text origin
  and swatch row within 1px).
- **Browser chrome color in dark.** Differed: the dark native capture showed
  light chrome, because the Chromium policy is machine-wide and the harness
  could not set it per instance. Tried: bwrap over the policy directory. The
  window mapped on the user's workspace because bwrap forks, so Hyprland's
  silent launch rule missed the pid. Then `unshare` (no fork) and a bind mount
  of a per-capture copy of the policy, with `setpriv` dropping capabilities.
  Moved: actual (the harness). Captures now use the mode's `chromium.theme`
  seed and scheme, and record `browser.chrome.follows_mode: true`. The machine
  policy is never written.
- **Browser chrome tones.** Differed: the recipe used palette shades (frame =
  background #F0EDEC, toolbar = lighter_background, omnibox =
  dark_background). Chromium ignores the seed's tone: in light the frame,
  tab, and toolbar are #FFFFFF, the omnibox is neutral #E1E1E2, and the
  new-tab button is warm #E2DDDA. In dark they are #443C37, #2E2B2B, and the
  seed itself. Tried: nothing. The seed is the only input, and the seed is
  the palette background by design. A different seed would shift every tone,
  not reproduce the palette. Moved: proposed. Measured per-mode tones in
  `[recipe.browser.<mode>]`; the dark new-tab button references `seed`.
  Sampled colors match the recipe exactly in dark and within 2 levels in
  light (0.985 opacity over the wallpaper).
- **Browser chrome structure and geometry.** Differed: the specimen had a
  second "New Tab" tab, 34/46px strips, a pill omnibox, no window close, and
  no star or profile. Helium has one tab (favicon, title, close), a 28px
  new-tab button, and a caption close. Its toolbar has back/forward
  (disabled), reload, an 8px-radius 28px omnibox (site info, URL with a muted
  path, star), extensions, profile, and menu. Below that is a transparent 1px
  line, then the page inset 4px with 8px corners. Tried: nothing on the actual
  side; it is Helium's layout. Moved: proposed (recipe geometry and specimen
  markup). The user's live extension icons are left out, and their slots stay
  empty so the omnibox keeps its width. Omnibox top/bottom/left/right within
  0.55px, content top/left within 0.5px (light; dark content top 1.16px, the
  transparent line shows a different backdrop), new-tab box within 0.45px.
  Every icon box is within 1px.
- **Browser UI type.** Differed: the specimen used 12–14px text. Chromium uses
  the GTK font `Inter 11` (14.67px) and rounds to whole px: 15px tab titles,
  14px omnibox. Tried: 14.67px everywhere (title 3px narrow, URL 14px wide).
  Moved: proposed (`font-size = 15`, `omnibox-font-size = 14`). This differs
  from the 12/14/16 UI sizes above because it is the system GTK font, not
  ours. The title box is within 1px. The URL runs 4–5px short over 375px
  because Chromium hints glyph advances at 1.5x.
- **Browser page.** Differed: the page started 14px lower and 4px further left
  in the specimen, because of the chrome height and inset. Tried: fixing the
  chrome only; the fixture page is shared. Moved: proposed. The h1 and
  paragraph boxes now match to 0px, and page RMSE is 32.9 → 10.0 (light) and
  27.1 → 8.4 (dark).

### OSD and notifications

The actual is the user's own `mikker.osd` and `mikker.notifications`
plugins. Measured with `script/align-check --surface osd notification`: the
native card and the specimen, rendered at the same 1.5x with the same 24px
margin, are both resampled to logical px. Landmarks are relative to the
card's inner keyline, and deltas are native − specimen. Card-interior RMSE
went from 68.9 to 14.1 (OSD, light), 60.3 to 49.1 (OSD, dark), 68.0 to 23.0
(notification, light), and 59.6 to 39.8 (notification, dark). Dark stays
high because the native dark card composites over the user's light desktop
(surface #555251 against #24201E in the specimen). That is the backdrop, not
the recipe.

- **OSD track.** Differed: the plugin drew a square 6px track in text at 0.45
  alpha. The specimen and reference B draw a 5px pill in text-primary at 0.14.
  Tried: new `recipe.osd` fields (`track`, `track-alpha`, `track-height`,
  `track-radius` → `radius.full`, `fill` → `control.<mode>.slider-fill`),
  generated into a new `[osd]` fragment (`shell.osd.toml`). `Osd.qml` reads
  them, with equal fallbacks. Moved: actual. The one exception is the fill's
  end. Qt cannot clip a child to its rounded parent without a mask, so the fill
  takes the track's radius. The specimen's fill is rounded too
  (`border-radius: inherit`, like the panel's range track), so the proposed
  side moved there. Track height is 4.63 against 4.67, length is 142 = 142, and
  the edges are within 0.52px.
- **OSD row geometry.** Differed: the specimen was a 285px-minimum grid with
  13px gaps and a 185px `1fr` track, 35px tall. The plugin pins an icon column
  as wide as the widest volume glyph's ink, uses 16px gaps and a 142px track,
  and gives the readout a right-aligned column as wide as "100%". Its row is
  display-large (33px) tall, so the card is 55px. Tried: nothing on the actual
  side. The pinned columns keep the track still while the volume changes.
  Moved: proposed. The numbers are now `recipe.osd` (`icon-size`, `gap`,
  `track-length`) and the plugin reads them, so both sides share one source.
  The specimen rebuilds the columns: the icon is `round(up, .5em, 1px)`, and a
  hidden "100%" sizer is rounded up with `calc-size()`, like Qt's `Math.ceil`.
  Chromium floors the 1px border to one device pixel at 1.5x, so the specimen
  card is 0.67px smaller each way. Card size is within 0.73 × 1.0px (light) and
  0.15 × 0.06px (dark). The track, icon, and readout boxes are within 1px.
- **OSD content.** Differed: the specimen had a "VOL" mono label at 62%. The
  plugin shows the volume glyph (U+F027 for `volume-medium`) and the
  fixture's 50%. Omarchy's OSD protocol has an icon and a value, not a label.
  Moved: proposed. The specimen draws the same glyph in `type.mono` at
  `icon-size` and shows 50%. The plugin's icon font was the `monospace` alias;
  it now reads `font.icon-family` (the same face today, and this README
  reserves Iosevka for glyphs). Moved: actual.
- **OSD glyph baseline.** Differed: the glyph sat 1.5–2px higher in the
  specimen. Iosevka's hhea, typo, and win metrics are identical (965/−285),
  and Qt's glyph already centres on the track within 0.5px. Tried: nothing
  further in Qt. Moved: proposed (a measured 1.5px CSS offset). The icon ink
  box is now within 0px (light) and 0.53px (dark).
- **Popup title type.** Differed: both plugins set Inter bold (700) on the OSD
  readout and the notification summary. The specimen used 11px mono at 600 for
  the readout and 13px bold for the summary. The README title role is 16px
  semibold with slightly tightened tracking, and 11px is not in the size
  hierarchy. Tried: `type.weight-semibold = 600` and `type.tracking-title =
  -0.01` (em), generated into `[font]` as `title-weight` and `title-tracking`,
  with `subtitle-weight` alongside. Omarchy's `Style` ignores these keys. The
  plugins set `font.weight` and `letterSpacing` (title size × tracking).
  `weight-strong` (650) would leave Qt choosing between two static faces.
  Moved: both, to the README. The actual changed weight and tracking; the
  proposed changed size and family, and the panel hero title got the same
  weight and tracking.
- **Notification body.** Differed: the plugin drew 16px regular in
  `Qt.darker(text, 1.15)`, a derived colour that no token holds. The specimen
  used 12px secondary at 400, and the README context subtitle is 12px medium
  secondary. Tried: `recipe.panel.text-secondary`, generated as
  `text-secondary` in `[popups]` and `[notifications]`. The card reads it
  (`Qt.darker` is only a fallback), with `Style.font.caption` and
  `subtitle-weight`. Moved: both. The actual took the colour, size, and weight;
  the specimen's subtitles went to 500. Sampled ink is #57666F against #51606A
  (light; thin 12px antialiasing) and #898F94 against #8C9398 (dark).
- **Notification padding, width, and gap.** Differed: the plugin used 12px
  horizontal and 10px vertical padding, a 380px width, and 2 + 2px between
  title and body. The specimen used 12px padding all round and a 282px width.
  The README popup padding is 14px. Tried: `Style.spacing.popupPadding` for
  multi-line toasts. Single-line glyph toasts keep their compact 7px vertical
  padding; neither the specimen nor the fixture shows one. The title gap is
  now `Style.spacing.labelGap` (4, the same value). Moved: actual for padding.
  For width, moved: proposed. 380px is upstream's width, shared with the
  history panel, and at 282px bodies would wrap into the 3-line clamp sooner.
  The width is now `recipe.notification.width`, which the card also reads.
  The specimen's gap became `label-gap` (it was 2px), and the specimen sits
  below the panel so it no longer covers the panel's hero. The card is within
  0.25 × 0.42px (light) and 0.57 × 0.1px (dark).
- **Notification line boxes.** Differed: the specimen body sat about 2px higher
  and the card was 2.4px shorter. At 1.5x, Qt rounds each line's ascent and
  descent up to whole device pixels: Inter at 16px is 16 + 4 = 20px, and at
  12px it is 12 + 3.33px. Chromium lays out 19.33px and 14px lines. Tried:
  nothing in Qt; this is its text engine. Moved: proposed (20px and 15.33px
  line heights in the specimen). Title and body line centres are within 0.59
  and 0.09px (light) and 1.0 and 0.5px (dark). Ink extents differ by a pixel
  because of hinting.
- **Notification icon and timestamp.** Differed: the specimen showed a 32px
  accent tile with a check and "now". The plugin draws a 40px app icon or
  image, or a glyph, only when the notification carries one. Popups show no
  time; the timestamp orders the history. Tried: giving the fixture an icon.
  The README has no notification-icon spec, and it reserves the accent for
  active, selected, focused, and actionable state, so an accent tile on every
  toast conflicts with it. Moved: proposed. The specimen shows the fixture's
  exact title and body. Glyph toasts now use `font.icon-family` instead of the
  bar family (actual; not captured).
- **Countdown.** Differed: `recipe.notification.countdown` (accent) is drawn
  on neither side. The plugin, like upstream `omarchy.notifications`, tracks
  the remaining lifetime but draws no bar. Moved: neither. The token remains
  the colour for when a countdown is drawn.
- **Keyline position.** Differed: the specimen's keyline sat 1.5–2px inside
  the edge (`outline-offset: -2px`). Every native surface (OSD, notification,
  bar tooltip) draws it at 1–1.5px, because the inner `BorderSurface` is inset
  by the border width. Moved: proposed (`-1.5px` on every floating surface).
- **Surface, edge, shadow, radius, and blur.** Differed: none of the values
  differ. Both sides read the background and alpha, the glass border and
  keyline, `shadow.floating`, and radius 14 (`Style.cornerRadius` is
  Hyprland's rounding, which is `radius.surface`). Blur is Hyprland's layer
  blur (size 16, 3 passes, in the handwritten `hyprland.lua`). No consumer
  reads `backdrop-blur`, and Hyprland's size is not a CSS blur radius. Moved:
  neither; noted.

New `[osd]`, `[font]`, and `text-secondary` keys reach the running shell only
after `omarchy theme refresh`, because the current theme is a copy. Until
then the plugins use their fallbacks: the numbers are identical, and the body
colour stays `Qt.darker`. Captures apply the generated payload in memory.
