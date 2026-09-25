const state = {
  saved: null,
  draft: null,
  raw: null,
  changes: {},
  paths: new Set(),
  readOnlySections: new Set(),
  provenance: {},
  manifest: null,
  savedIssues: [],
  draftIssues: null,
  validation: { timer: null, sequence: 0, failed: false },
  inspecting: null,
  inspectHistory: [],
  adapterOpen: new Map(),
  busy: false,
  // A static export (GitHub Pages): no backend, so nothing saves, validates, or captures.
  snapshot: false,
  view: "shell",
  mode: "both",
};

const descriptions = {
  shell: ["Shell specimens", "Fast reconstructions driven directly by canonical tokens."],
  hunk: ["Hunk specimen", "The code-review adapter rendered against a fixed diff."],
  windows: ["Window specimens", "Tiled Hyprland windows, a ghostty terminal, and a Helium browser against fixed fixtures."],
  foundations: ["Foundations", "Color, type, and hierarchy without consumer-specific decoration."],
  adapters: ["Adapters", "Every generated target and the recipe fields it maps from canonical tokens."],
  captures: ["Native captures", "Real Omarchy surfaces captured with grim beside their live specimens."],
};

const layers = {
  palette: "foundation",
  "wide-gamut": "foundation",
  type: "foundation",
  spacing: "foundation",
  radius: "foundation",
  border: "foundation",
  shadow: "foundation",
  blur: "foundation",
  window: "foundation",
  motion: "foundation",
  color: "semantic",
  control: "semantic",
  chrome: "semantic",
  syntax: "semantic",
  recipe: "recipe",
  meta: "meta",
};

const consumers = {
  "omarchy-theme": ["Omarchy theme", "Section-replacing TOML fragments for popups, notifications, tooltips, controls, bar, menu, launcher, and borders."],
  "omarchy-plugin": ["Omarchy plugins", "QML plugin sources that read the generated Omarchy theme at runtime; they map no token fields directly."],
  hunk: ["Hunk", "Light and dark semantic and syntax themes for the code-review TUI."],
  "studio-css": ["Studio CSS", "CSS custom properties for every scalar token, consumed by this studio and the HTML references."],
};

const emptyTargetNotes = {
  "omarchy-plugin": "Reads the generated Omarchy theme at runtime; no token fields are written here.",
  "studio-css": "Publishes every scalar token as a CSS custom property; no per-field recipe mapping.",
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const clone = value => JSON.parse(JSON.stringify(value));
const has = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const hasChanges = () => Object.keys(state.changes).length > 0;

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.entries(props).forEach(([key, value]) => {
    if (value === undefined || value === null || value === false) return;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  });
  node.append(...children.flat(Infinity).filter(child => child !== null && child !== undefined && child !== false));
  return node;
}

function getPath(object, path) {
  return path.split(".").reduce((current, part) => current?.[part], object);
}

function setPath(object, path, value) {
  const parts = path.split(".");
  const key = parts.pop();
  const parent = parts.reduce((current, part) => current?.[part], object);
  if (parent && typeof parent === "object") parent[key] = value;
}

function flatten(object, prefix = "") {
  return Object.entries(object).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return value && typeof value === "object" && !Array.isArray(value)
      ? flatten(value, path)
      : [{ path, key, value }];
  });
}

function friendly(value) {
  return value.replaceAll("-", " ").replaceAll("_", " ").replace(/\b\w/g, letter => letter.toUpperCase());
}

function isColor(value) {
  return typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value);
}

function isReference(value) {
  return typeof value === "string" && value.startsWith("@") && !value.includes("{");
}

function layerOf(path) {
  return layers[path.split(".")[0]] || "token";
}

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

// "section.key" for a generated field; either part may be missing.
function fieldLabel(field) {
  return [field.section, field.key].filter(part => part !== null && part !== undefined && part !== "").join(".");
}

function colorWithAlpha(color, alpha) {
  if (!isColor(color)) return color;
  const [red, green, blue] = [1, 3, 5].map(index => parseInt(color.slice(index, index + 2), 16));
  return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
}

function luminance(color) {
  const channels = [1, 3, 5].map(index => parseInt(color.slice(index, index + 2), 16) / 255);
  const linear = channels.map(channel => channel <= .03928 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4);
  return linear[0] * .2126 + linear[1] * .7152 + linear[2] * .0722;
}

function contrastInk(color) {
  return isColor(color) && luminance(color) > .46 ? "#253038" : "#fff";
}

function contrastRatio(first, second) {
  const [light, dark] = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (light + .05) / (dark + .05);
}

// Raw values carry references ("@palette.light.accent"); the draft holds resolved scalars.
function savedRaw(path) {
  return getPath(state.raw, path);
}

function draftRaw(path) {
  return has(state.changes, path) ? state.changes[path] : savedRaw(path);
}

function resolvePath(path, depth = 0) {
  if (depth > 24) return undefined;
  const value = draftRaw(path);
  return isReference(value) ? resolvePath(value.slice(1), depth + 1) : value;
}

function rebuildDraft() {
  const draft = clone(state.saved);
  Object.entries(state.raw).forEach(([section, content]) => {
    if (state.readOnlySections.has(section) || !content || typeof content !== "object") return;
    flatten(content, section).forEach(({ path }) => {
      const value = resolvePath(path);
      if (value !== undefined && getPath(draft, path) !== undefined) setPath(draft, path, value);
    });
  });
  state.draft = draft;
}

function displayValue(value) {
  if (value === undefined) return "—";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function valueChip(value) {
  const chip = el("span", { class: "value-chip", title: displayValue(value) });
  if (isColor(value)) chip.append(el("i", { class: "swatch-dot", style: `background:${value}` }));
  chip.append(el("code", { text: displayValue(value) }));
  return chip;
}

function tokenButton(path) {
  return el("button", {
    class: "path-link",
    type: "button",
    text: path,
    title: `Jump to ${path}`,
    onclick: event => {
      event.preventDefault();
      jumpToToken(path);
    },
  });
}

// Inspector ----------------------------------------------------------------

function tokenInput(token) {
  const control = el("div", { class: "token-control" });

  if (typeof token.value === "boolean") {
    control.classList.add("boolean-control");
    const input = el("input", { type: "checkbox", dataset: { path: token.path } });
    input.checked = token.value;
    input.addEventListener("change", () => changeToken(token.path, input.checked));
    control.append(input);
    return control;
  }

  const numeric = typeof token.value === "number";
  const input = el("input", { type: numeric ? "number" : "text", dataset: { path: token.path }, spellcheck: "false" });
  input.value = token.value;
  if (numeric) input.step = Number.isInteger(token.value) ? "1" : "0.01";

  const update = () => {
    let value = input.value;
    if (numeric) {
      value = Number(value);
      if (input.value.trim() === "" || !Number.isFinite(value)) return;
      if (Number.isInteger(token.value)) value = Math.round(value);
    }
    changeToken(token.path, value);
  };
  input.addEventListener("input", update);

  if (isColor(token.value)) {
    control.classList.add("color-control");
    const picker = el("input", { type: "color", dataset: { pickerFor: token.path }, "aria-label": `${token.path} color` });
    picker.value = token.value.toLowerCase();
    picker.addEventListener("input", () => {
      input.value = picker.value.toUpperCase();
      update();
    });
    input.addEventListener("input", () => {
      if (isColor(input.value)) picker.value = input.value.toLowerCase();
    });
    control.append(picker, input);
  } else {
    control.append(input);
  }
  return control;
}

function linkLine(path) {
  const current = draftRaw(path);
  const original = savedRaw(path);
  if (isReference(current)) {
    return el("div", { class: "link-line" },
      el("span", { class: "link-badge", text: "linked", title: `Resolves from ${current.slice(1)}` }),
      tokenButton(current.slice(1)));
  }
  if (isReference(original)) {
    return el("div", { class: "link-line" },
      el("span", { class: "link-badge is-unlinked", text: "unlinked", title: `Saved as a reference to ${original.slice(1)}; this edit replaces it with a literal` }),
      el("button", {
        class: "relink",
        type: "button",
        title: `Restore the reference to ${original.slice(1)}`,
        onclick: event => {
          event.preventDefault();
          changeToken(path, original);
        },
      }, "relink to ", el("code", { text: original.slice(1) })));
  }
  return null;
}

function tokenRow(token, readOnly) {
  const row = el("div", { class: `token-row${readOnly ? " is-readonly" : ""}`, dataset: { path: token.path } },
    el("div", { class: "token-name" },
      el("span", { class: "token-title" },
        el("span", { class: "token-label", text: friendly(token.key) }),
        el("button", {
          class: "provenance-button",
          type: "button",
          text: "↗",
          title: `Provenance of ${token.path}`,
          "aria-label": `Show provenance for ${token.path}`,
          onclick: () => openProvenance(token.path, false),
        })),
      el("small", { text: token.path })));
  if (readOnly) {
    const raw = savedRaw(token.path);
    row.append(el("div", { class: "token-readonly", title: `${displayValue(raw)} (read-only)` }, valueChip(raw ?? token.value)));
  } else {
    row.append(tokenInput(token));
    refreshRow(row);
  }
  return row;
}

// Link line and change marks of an editable row.
function refreshRow(row) {
  const path = row.dataset.path;
  const name = $(".token-name", row);
  $(".link-line", name)?.remove();
  const line = linkLine(path);
  if (line) name.append(line);
  row.classList.toggle("is-linked", isReference(draftRaw(path)));
  row.classList.toggle("is-unlinked", !isReference(draftRaw(path)) && isReference(savedRaw(path)));
  row.classList.toggle("is-changed", has(state.changes, path));
}

function renderInspector() {
  const target = $("#token-groups");
  target.replaceChildren();
  const query = $("#token-search").value.trim().toLowerCase();
  let visible = 0;

  Object.entries(state.draft).forEach(([section, content], sectionIndex) => {
    if (!content || typeof content !== "object") return;
    const readOnly = state.readOnlySections.has(section);
    const source = readOnly ? state.raw[section] ?? content : content;
    const tokens = flatten(source, section).filter(token => token.path.toLowerCase().includes(query));
    if (!tokens.length) return;
    visible += tokens.length;

    const group = el("details", { class: "token-group", dataset: { section } },
      el("summary", {},
        el("span", { text: friendly(section) }),
        readOnly ? el("span", { class: "readonly-tag", text: "read-only" }) : null,
        el("span", { class: "token-count", text: tokens.length })),
      el("div", { class: "token-list" }, tokens.map(token => tokenRow(token, readOnly))));
    group.open = Boolean(query) || ["color", "type"].includes(section) || sectionIndex === 0;
    target.append(group);
  });

  if (!visible) target.append(el("div", { class: "empty-filter", text: "No matching tokens." }));
  renderIssues();
  markInspecting();
}

// Bring editable rows in line with the draft without rebuilding them (keeps focus).
function syncInspector() {
  $$(".token-row:not(.is-readonly)").forEach(row => {
    const value = getPath(state.draft, row.dataset.path);
    const input = $("input[data-path]", row);
    if (input !== document.activeElement) {
      if (input.type === "checkbox") input.checked = Boolean(value);
      else if (String(input.value) !== String(value)) input.value = value;
    }
    const picker = $("input[type=color]", row);
    if (picker && picker !== document.activeElement && isColor(value)) picker.value = value.toLowerCase();
    refreshRow(row);
  });
}

function changeToken(path, value) {
  if (savedRaw(path) === value) delete state.changes[path];
  else state.changes[path] = value;
  draftChanged();
}

// Everything that follows the draft: rows, specimens, open panels, and validation.
function draftChanged() {
  rebuildDraft();
  syncInspector();
  renderPreviews();
  if (state.view === "adapters") renderAdapters();
  renderProvenance();
  updateDirtyState();
  scheduleValidation();
}

// Specimen variables -------------------------------------------------------

const px = value => (typeof value === "number" ? `${value}px` : undefined);
const fontFamily = value => (typeof value === "string" ? JSON.stringify(value) : undefined);

// Recipes stay templates in the draft: resolve one field for a mode, honouring
// [recipe.<name>.<mode>] overrides and "{mode}" references, as tokens_model.py does.
function recipeValue(name, field, mode, depth = 0) {
  if (depth > 24) return undefined;
  const override = draftRaw(`recipe.${name}.${mode}.${field}`);
  const value = override !== undefined ? override : draftRaw(`recipe.${name}.${field}`);
  if (typeof value !== "string" || !value.startsWith("@")) return value;
  const target = value.slice(1).replaceAll("{mode}", mode);
  const parts = target.split(".");
  if (parts[0] === "recipe" && parts.length === 3) return recipeValue(parts[1], parts[2], mode, depth + 1);
  return resolvePath(target);
}

// Shell and Hunk specimens: semantic colors, glass, spacing, and type.
function shellVariables(mode) {
  const { color: colors, control: controls, border: borders, shadow: shadows, radius, blur, spacing, type } = state.draft;
  const color = colors[mode];
  const border = borders[mode === "dark" ? "glass-dark" : "glass"];
  const shadow = shadows.floating;
  return {
    "--canvas": color.canvas,
    "--surface-composed": colorWithAlpha(color["surface-raised"], color["surface-raised-alpha"]),
    "--surface-alt": color["surface-alt"],
    "--text": color["text-primary"],
    "--secondary": color["text-secondary"],
    "--muted": color["text-muted"],
    "--accent": color.accent,
    "--border": color["border-solid"],
    "--note-border": color["note-border"],
    "--add": color["diff-add"],
    "--delete": color["diff-delete"],
    "--glass-border": colorWithAlpha(border.color, border.alpha),
    "--inner-border": colorWithAlpha(border["inner-color"], border["inner-alpha"]),
    "--shadow-color": colorWithAlpha(shadow.color, shadow.alpha),
    "--shadow-blur": px(shadow.blur),
    "--radius": px(radius.surface),
    "--control-radius": px(radius.control),
    "--blur": px(blur.floating),
    "--panel-padding": px(spacing["panel-padding"]),
    "--row-padding-x": px(spacing["row-padding-x"]),
    "--row-gap": px(spacing["row-gap"]),
    "--control-height": px(spacing["control-height"]),
    "--osd-padding-x": px(spacing["osd-padding-x"]),
    "--osd-padding-y": px(spacing["osd-padding-y"]),
    "--small-size": px(type["size-small"]),
    "--title-size": px(type["size-title"]),
    "--ui-font": JSON.stringify(type.ui),
    "--mono-font": JSON.stringify(type.mono),
    "--selected-fill": colorWithAlpha(controls[mode]["primary-background"], controls[mode]["selected-fill-alpha"]),
  };
}

// The bar recipe. A transparent bar (shell.json `bar.transparent`) paints no
// surface; like omarchy-bar-text-color it uses `text` or `transparent-text`,
// whichever contrasts more with what is behind it (here the specimen canvas,
// natively the wallpaper under the bar).
function barVariables(mode) {
  const bar = field => recipeValue("bar", field, mode);
  const transparent = bar("transparent") === true;
  const behind = resolvePath(`color.${mode}.canvas`);
  let text = bar("text");
  const alternative = bar("transparent-text");
  if (transparent && [text, alternative, behind].every(isColor)
      && contrastRatio(alternative, behind) > contrastRatio(text, behind)) text = alternative;
  return {
    "--bar-text": text,
    "--bar-surface": transparent ? "transparent" : colorWithAlpha(bar("background"), bar("background-alpha")),
    "--bar-font": fontFamily(bar("font-family")),
    "--bar-font-size": px(bar("font-size")),
    "--bar-height": px(bar("size-horizontal")),
  };
}

// Popup typography roles, the notification card, and the OSD row (recipe.osd).
function popupVariables(mode) {
  const osd = field => recipeValue("osd", field, mode);
  const tracking = resolvePath("type.tracking-title");
  return {
    "--title-weight": resolvePath("type.weight-semibold"),
    "--title-tracking": typeof tracking === "number" ? `${tracking}em` : undefined,
    "--subtitle-weight": resolvePath("type.weight-medium"),
    "--popup-padding": px(resolvePath("spacing.popup-padding")),
    "--label-gap": px(resolvePath("spacing.label-gap")),
    "--notification-width": px(recipeValue("notification", "width", mode)),
    "--osd-icon-size": px(osd("icon-size")),
    "--osd-gap": px(osd("gap")),
    "--osd-track": colorWithAlpha(osd("track"), osd("track-alpha")),
    "--osd-track-height": px(osd("track-height")),
    "--osd-track-length": px(osd("track-length")),
    "--osd-track-radius": px(osd("track-radius")),
    "--osd-fill": osd("fill"),
  };
}

// Hyprland window decoration (recipe.window).
function windowVariables(mode) {
  const window = field => recipeValue("window", field, mode);
  return {
    "--window-border-width": px(window("border-width")),
    "--window-active-border": window("active-border"),
    "--window-inactive-border": colorWithAlpha(window("inactive-border"), window("inactive-border-alpha")),
    "--window-rounding": px(window("rounding")),
    "--window-gaps-inner": px(window("gaps-inner")),
    "--window-gaps-outer": px(window("gaps-outer")),
    "--window-gaps-outer-top": px(window("gaps-outer-top")),
    "--window-shadow-active": colorWithAlpha(window("shadow-color"), window("shadow-alpha")),
    "--window-shadow-inactive": colorWithAlpha(window("shadow-color"), window("shadow-alpha-inactive")),
    "--window-shadow-range": px(window("shadow-range")),
    "--window-shadow-x": px(window("shadow-x")),
    "--window-shadow-y": px(window("shadow-y")),
    "--window-opacity-active": window("opacity-active"),
    "--window-opacity-inactive": window("opacity-inactive"),
  };
}

// The ghostty terminal (recipe.terminal).
function terminalVariables(mode) {
  const terminal = field => recipeValue("terminal", field, mode);
  const size = terminal("font-size");
  const scale = terminal("font-scale");
  const values = {
    "--terminal-background": terminal("background"),
    "--terminal-foreground": terminal("foreground"),
    "--terminal-font": fontFamily(terminal("font-family")),
    // ghostty's font-size is in points (96 dpi: 1pt = 4/3 px) times the desktop text scale; the grid is
    // ghostty's rounded cell.
    "--terminal-size": typeof size === "number" ? `${(size * 4 * (typeof scale === "number" ? scale : 1)) / 3}px` : undefined,
    "--terminal-cell-width": px(terminal("cell-width")),
    "--terminal-cell-height": px(terminal("cell-height")),
    "--terminal-padding-x": px(terminal("padding-x")),
    "--terminal-padding-top": px(terminal("padding-top")),
    "--terminal-padding-bottom": px(terminal("padding-bottom")),
  };
  for (let index = 0; index < 16; index += 1) values[`--ansi-${index}`] = terminal(`ansi-${index}`);
  return values;
}

// The Helium browser chrome (recipe.browser).
function browserVariables(mode) {
  const browser = field => recipeValue("browser", field, mode);
  return {
    "--browser-frame": browser("frame"),
    "--browser-toolbar": browser("toolbar"),
    "--browser-active-tab": browser("active-tab"),
    "--browser-omnibox": browser("omnibox"),
    "--browser-new-tab": browser("new-tab-background"),
    "--browser-text": browser("text"),
    "--browser-text-muted": browser("text-muted"),
    "--browser-icon-disabled": browser("icon-disabled"),
    "--browser-caption": browser("caption"),
    "--browser-avatar": browser("avatar"),
    "--browser-font": fontFamily(browser("font-family")),
    "--browser-font-size": px(browser("font-size")),
    "--browser-omnibox-font-size": px(browser("omnibox-font-size")),
    "--browser-tab-strip-height": px(browser("tab-strip-height")),
    "--browser-tab-width": px(browser("tab-width")),
    "--browser-toolbar-height": px(browser("toolbar-height")),
    "--browser-separator-width": px(browser("separator-width")),
    "--browser-omnibox-height": px(browser("omnibox-height")),
    "--browser-omnibox-radius": px(browser("omnibox-radius")),
    "--browser-new-tab-height": px(browser("new-tab-height")),
    "--browser-new-tab-radius": px(browser("new-tab-radius")),
    "--browser-content-inset": px(browser("content-inset")),
    "--browser-content-radius": px(browser("content-radius")),
    "--browser-opacity-active": browser("opacity-active"),
    "--browser-opacity-inactive": browser("opacity-inactive"),
  };
}

// Style a light or dark specimen root; an unresolvable value falls back to the stylesheet default.
function setVariables(element, mode) {
  const transparentBar = recipeValue("bar", "transparent", mode) === true;
  element.querySelectorAll(".menubar").forEach(node => node.classList.toggle("is-transparent", transparentBar));
  const values = {
    ...shellVariables(mode), ...barVariables(mode), ...popupVariables(mode),
    ...windowVariables(mode), ...terminalVariables(mode), ...browserVariables(mode),
  };
  Object.entries(values).forEach(([name, value]) => {
    if (value === undefined || value === null) element.style.removeProperty(name);
    else element.style.setProperty(name, String(value));
  });
}

// Window specimens -----------------------------------------------------------

// The terminal fixture is ANSI text so the native capture can `cat` the same bytes.
const TERMINAL_FIXTURE = "fixtures/terminal-session.ans";
// The native browser capture loads the fixture page from the default studio (captures.py DEFAULT_STUDIO_URL).
const BROWSER_FIXTURE_HOST = "127.0.0.1";
const BROWSER_FIXTURE_PORT = 4777;

// Fill every empty [data-fill] host from its <template>, outermost first (templates nest).
function fillSpecimens() {
  for (let host = $("[data-fill]:empty"); host; host = $("[data-fill]:empty")) {
    host.append(document.getElementById(`${host.dataset.fill}-template`).content.cloneNode(true));
  }
  $$("iframe[data-src]").forEach(frame => {
    const mode = frame.closest(".dark-preview") ? "dark" : "light";
    frame.src = `${frame.dataset.src}?scheme=${mode}`;
    // Chromium's omnibox shows the host at full strength and the rest of the URL muted. The
    // URL is the default studio's (what `script/capture` loads), not this page's, so
    // specimens render the same on every studio instance and in a static export.
    $(".browser-url", frame.closest(".browser-specimen")).replaceChildren(
      el("span", { class: "browser-url-host", text: BROWSER_FIXTURE_HOST }),
      `:${BROWSER_FIXTURE_PORT}/${frame.dataset.src}?scheme=${mode}`);
  });
}

// Minimal SGR renderer: reset (0), bold (1/22), and the 16 ANSI foregrounds (30-37, 90-97, 39).
function ansiFragment(text) {
  const fragment = document.createDocumentFragment();
  const pattern = /\x1b\[([0-9;]*)m/g;
  let color = null;
  let bold = false;
  let position = 0;
  const emit = chunk => {
    if (!chunk) return;
    if (color === null && !bold) {
      fragment.append(chunk);
      return;
    }
    const classes = [color === null ? null : `ansi-${color}`, bold ? "ansi-bold" : null].filter(Boolean);
    fragment.append(el("span", { class: classes.join(" "), text: chunk }));
  };
  for (const match of text.matchAll(pattern)) {
    emit(text.slice(position, match.index));
    position = match.index + match[0].length;
    (match[1] === "" ? ["0"] : match[1].split(";")).map(Number).forEach(code => {
      if (code === 0) { color = null; bold = false; }
      else if (code === 1) bold = true;
      else if (code === 22) bold = false;
      else if (code >= 30 && code <= 37) color = code - 30;
      else if (code >= 90 && code <= 97) color = code - 90 + 8;
      else if (code === 39) color = null;
    });
  }
  emit(text.slice(position));
  return fragment;
}

async function loadTerminalFixture() {
  try {
    const response = await fetch(TERMINAL_FIXTURE, { cache: "no-cache" });
    if (!response.ok) throw new Error(`Terminal fixture unavailable (${response.status})`);
    const text = (await response.text()).replace(/\n$/, "");
    $$(".terminal-screen").forEach(screen => {
      screen.replaceChildren(ansiFragment(text));
      screen.dataset.ready = "true";
    });
  } catch (error) {
    showNotice(error.message, true);
  }
}

function renderFoundations() {
  $("#foundation-grid").replaceChildren(...["light", "dark"].map(mode =>
    el("article", { class: "foundation-mode", dataset: { previewMode: mode } },
      el("div", { class: "foundation-head" }, el("strong", { text: friendly(mode) }), el("span", { text: "semantic color roles" })),
      el("div", { class: "swatch-grid" },
        Object.entries(state.draft.color[mode]).filter(([, value]) => isColor(value)).map(([name, value]) => {
          const swatch = el("div", { class: "swatch" }, el("strong", { text: name }), el("code", { text: value }));
          swatch.style.background = value;
          swatch.style.setProperty("--swatch-ink", contrastInk(value));
          return swatch;
        })))));
}

function renderPreviews() {
  $$(".light-preview").forEach(element => setVariables(element, "light"));
  $$(".dark-preview").forEach(element => setVariables(element, "dark"));
  renderFoundations();
  const accent = state.draft.color.light.accent;
  if (isColor(accent)) document.documentElement.style.setProperty("--studio-accent", accent);
}

// Validation ---------------------------------------------------------------

function currentIssues() {
  return state.draftIssues ?? state.savedIssues;
}

function errorCount(issues) {
  return issues.filter(issue => issue.level === "error").length;
}

function issueSummary(issues) {
  const errors = errorCount(issues);
  const warnings = issues.length - errors;
  return [errors && plural(errors, "error"), warnings && plural(warnings, "warning")].filter(Boolean).join(" · ");
}

function issueLine(issue) {
  return el("div", { class: `issue-line is-${issue.level === "error" ? "error" : "warning"}` },
    el("span", { class: "issue-code", text: issue.code || issue.level }),
    el("span", { class: "issue-message", text: issue.message || "Invalid value" }));
}

function renderIssues() {
  const issues = currentIssues();
  const byPath = new Map();
  const general = [];
  issues.forEach(issue => {
    if (issue.path && state.paths.has(issue.path)) {
      if (!byPath.has(issue.path)) byPath.set(issue.path, []);
      byPath.get(issue.path).push(issue);
    } else {
      general.push(issue);
    }
  });

  $$(".token-row").forEach(row => {
    row.classList.remove("has-error", "has-warning");
    row.removeAttribute("title");
    $(".token-issues", row)?.remove();
    const rowIssues = byPath.get(row.dataset.path);
    if (!rowIssues) return;
    row.classList.add(errorCount(rowIssues) ? "has-error" : "has-warning");
    row.title = rowIssues.map(issue => issue.message).join("\n");
    row.append(el("div", { class: "token-issues" }, rowIssues.map(issueLine)));
  });

  $$(".token-group").forEach(group => {
    const sectionIssues = issues.filter(issue => issue.path?.split(".")[0] === group.dataset.section);
    const error = errorCount(sectionIssues) > 0;
    const summary = $("summary", group);
    summary.classList.toggle("has-error", error);
    summary.classList.toggle("has-warning", sectionIssues.length > 0 && !error);
  });

  const list = $("#general-issues");
  list.hidden = !general.length;
  list.replaceChildren(...(general.length ? [
    el("div", { class: "general-issues-head", text: `Source ${general.length === 1 ? "issue" : "issues"}` }),
    ...general.map(issue => {
      const line = issueLine(issue);
      if (issue.path) line.append(el("code", { class: "issue-path", text: issue.path }));
      return line;
    }),
  ] : []));

  const summary = $("#issue-summary");
  const errors = errorCount(issues);
  summary.hidden = !issues.length && !state.validation.failed;
  summary.className = `issue-summary${errors ? " has-errors" : issues.length ? " has-warnings" : ""}`;
  summary.textContent = issues.length ? issueSummary(issues) : state.validation.failed ? "validation unavailable" : "";
  summary.title = issues.length
    ? `${state.draftIssues ? "Draft" : "Saved source"}: ${issues.map(issue => `${issue.path || "source"} — ${issue.message}`).join("\n")}`
    : "";
  updateActions();
}

function scheduleValidation() {
  clearTimeout(state.validation.timer);
  state.validation.sequence += 1;
  if (!hasChanges()) {
    state.draftIssues = null;
    renderIssues();
    return;
  }
  if (!state.snapshot) state.validation.timer = setTimeout(validateDraft, 250);
}

async function validateDraft() {
  const sequence = state.validation.sequence;
  try {
    const payload = await request("api/validate", { changes: state.changes });
    if (sequence !== state.validation.sequence) return;
    state.validation.failed = false;
    state.draftIssues = payload.issues;
  } catch {
    if (sequence !== state.validation.sequence) return;
    state.validation.failed = true;
  }
  renderIssues();
}

function updateActions() {
  const errors = errorCount(currentIssues());
  [
    [$("#save"), "saving", "Write the canonical TOML and regenerate adapters"],
    [$("#apply"), "applying", "Save, regenerate, and reload Omarchy Shell"],
  ].forEach(([button, verb, title]) => {
    button.disabled = state.busy || errors > 0;
    button.classList.toggle("is-blocked", errors > 0 && !state.busy);
    button.title = errors ? `Fix ${plural(errors, "validation error")} before ${verb}` : title;
  });
}

// State --------------------------------------------------------------------

function updateDirtyState(status = null) {
  const target = $("#save-state");
  const count = Object.keys(state.changes).length;
  target.className = "save-state";
  if (status) {
    target.textContent = status;
  } else if (count) {
    target.textContent = `${count} unsaved ${count === 1 ? "change" : "changes"}`;
    target.classList.add("is-dirty");
  } else {
    target.textContent = state.snapshot ? "Read-only snapshot" : "Source is current";
    target.classList.add("is-saved");
  }
}

function showNotice(message, error = false) {
  const notice = $("#notice");
  notice.hidden = false;
  notice.className = `notice${error ? " is-error" : ""}`;
  notice.textContent = message;
  clearTimeout(showNotice.timer);
  showNotice.timer = setTimeout(() => notice.hidden = true, 5500);
}

// GET (or POST a JSON body) and return the JSON payload; throws on HTTP, parse, or `ok: false` errors.
async function request(path, body = null) {
  const response = await fetch(path, body === null ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok || !payload || payload.ok === false) {
    const error = new Error(payload?.error || `Request failed: ${response.status}`);
    error.issues = payload?.issues ?? null;
    throw error;
  }
  return payload;
}

function acceptState(payload) {
  state.saved = payload.tokens;
  state.raw = payload.raw;
  state.readOnlySections = new Set(payload.readOnlySections);
  state.provenance = payload.provenance;
  state.manifest = payload.manifest;
  state.savedIssues = payload.issues;
  state.draftIssues = null;
  state.changes = {};
  state.paths = new Set([...flatten(state.saved), ...flatten(state.raw)].map(token => token.path));
  $("#source-path").textContent = payload.source;
  $("#source-path").title = payload.source;
  rebuildDraft();
  renderInspector();
  draftChanged();
}

// The live studio serves api/state; a static export (script/design-system export) ships the
// same payload as api/state.json and becomes a read-only snapshot.
async function load() {
  let payload;
  try {
    payload = await request("api/state");
  } catch (error) {
    try {
      payload = await request("api/state.json");
    } catch {
      showNotice(error.message, true);
      updateDirtyState("Source unavailable");
      return;
    }
    enterSnapshot(payload);
  }
  acceptState(payload);
}

function enterSnapshot(payload) {
  state.snapshot = true;
  document.body.classList.add("is-snapshot");
  $("#save").hidden = true;
  $("#apply").hidden = true;
  $("#reset").title = "Discard draft edits";
  const commit = typeof payload.commit === "string" ? payload.commit.slice(0, 10) : null;
  $("#snapshot-banner").replaceChildren(el("span", {},
    el("strong", { text: "Read-only snapshot." }),
    " Token edits preview here and are never saved.",
    payload.source ? [" Source ", el("code", { text: payload.source })] : null,
    commit ? [" at ", el("code", { text: commit })] : null));
  $("#snapshot-banner").hidden = false;
}

function busy(active) {
  state.busy = active;
  updateActions();
}

function rejectRequest(error) {
  if (error.issues) {
    if (hasChanges()) state.draftIssues = error.issues;
    else state.savedIssues = error.issues;
    renderIssues();
    const summary = issueSummary(error.issues);
    showNotice(summary ? `${error.message}: ${summary}. Fix the marked tokens and try again.` : error.message, true);
  } else {
    showNotice(error.message, true);
  }
  updateDirtyState();
}

// Show the change report (web/report.js) first; resolves true to proceed.
async function confirmChanges(action) {
  busy(true);
  try {
    return await WhitePillReport.confirm(state.changes, action);
  } finally {
    busy(false);
  }
}

async function save() {
  if (!hasChanges()) {
    showNotice("Nothing to save. Canonical tokens are already current.");
    return;
  }
  if (!(await confirmChanges("save"))) return;
  busy(true);
  updateDirtyState("Generating…");
  try {
    acceptState(await request("api/save", { changes: state.changes }));
    showNotice("Canonical tokens saved and downstream adapters generated.");
  } catch (error) {
    rejectRequest(error);
  } finally {
    busy(false);
  }
}

async function apply() {
  if (!(await confirmChanges("apply"))) return;
  busy(true);
  try {
    if (hasChanges()) {
      updateDirtyState("Saving…");
      acceptState(await request("api/save", { changes: state.changes }));
    }
    updateDirtyState("Applying…");
    acceptState(await request("api/apply", {}));
    showNotice("Generated adapters applied and the current Omarchy theme refreshed.");
  } catch (error) {
    rejectRequest(error);
  } finally {
    busy(false);
  }
}

function reset() {
  state.changes = {};
  draftChanged();
}

// Navigation and provenance ------------------------------------------------

function rowFor(path) {
  return $(`.token-row[data-path="${CSS.escape(path)}"]`);
}

function jumpToToken(path) {
  let row = rowFor(path);
  if (!row && $("#token-search").value) {
    $("#token-search").value = "";
    renderInspector();
    row = rowFor(path);
  }
  if (!row) {
    showNotice(`${path} is not an inspector token.`, true);
    return;
  }
  row.closest("details").open = true;
  row.scrollIntoView({ block: "center", behavior: "smooth" });
  row.classList.remove("is-flash");
  void row.offsetWidth;
  row.classList.add("is-flash");
}

function markInspecting() {
  $$(".token-row.is-inspecting").forEach(row => row.classList.remove("is-inspecting"));
  if (state.inspecting) rowFor(state.inspecting)?.classList.add("is-inspecting");
}

// From a row button (remember = false) the history restarts; following a hop remembers the current token.
function openProvenance(path, remember = true) {
  if (!remember) state.inspectHistory = [];
  else if (state.inspecting && state.inspecting !== path) state.inspectHistory.push(state.inspecting);
  state.inspecting = path;
  $("#provenance").hidden = false;
  renderProvenance();
  markInspecting();
}

function closeProvenance() {
  state.inspecting = null;
  state.inspectHistory = [];
  $("#provenance").hidden = true;
  markInspecting();
}

function followHop(path) {
  openProvenance(path);
  jumpToToken(path);
}

function downstream(path) {
  // This token's own uses first, then uses reached through tokens that reference it.
  // Merged per recipe field so a use reported both ways is listed once.
  const entries = new Map();
  const seen = new Set();
  const queue = [[path, null]];
  while (queue.length) {
    const [current, via] = queue.shift();
    if (seen.has(current)) continue;
    seen.add(current);
    const info = state.provenance[current];
    if (!info) continue;
    info.usedBy.forEach(use => {
      const key = `${use.recipe ?? ""}\u0000${use.field ?? ""}`;
      if (!entries.has(key)) entries.set(key, { recipe: use.recipe, field: use.field, via, consumers: [], keys: new Set() });
      const entry = entries.get(key);
      use.consumers.forEach(consumer => {
        const id = `${consumer.display}\u0000${consumer.section}\u0000${consumer.key}`;
        if (entry.keys.has(id)) return;
        entry.keys.add(id);
        entry.consumers.push(consumer);
      });
    });
    info.referencedBy.forEach(next => queue.push([next, via ?? next]));
  }
  return [...entries.values()];
}

function consumerItem(consumer) {
  return el("li", { title: consumer.display },
    el("code", { class: "consumer-key", text: fieldLabel(consumer) }),
    el("span", { class: "consumer-file", text: consumer.display }));
}

function hopItem(path, self = false) {
  const layer = layerOf(path);
  const value = getPath(state.draft, path) ?? savedRaw(path);
  return el(self ? "div" : "button", {
    class: `hop${self ? " is-self" : ""}`,
    type: self ? null : "button",
    title: self ? null : `Inspect ${path}`,
    onclick: self ? null : () => followHop(path),
  },
  el("span", { class: `layer-tag is-${layer}`, text: layer }),
  el("code", { class: "hop-path", text: path }),
  valueChip(value));
}

function provenanceSection(title, count, ...content) {
  return el("section", { class: "provenance-section" }, el("h4", {}, title, el("span", { text: count })), content);
}

function linkStatus(path, current, original) {
  if (isReference(current)) return el("span", { class: "link-badge", text: `linked → ${current.slice(1)}` });
  if (isReference(original)) return el("span", { class: "link-badge is-unlinked", text: "unlinked (unsaved)" });
  return el("span", { class: "link-badge is-muted", text: state.readOnlySections.has(path.split(".")[0]) ? "read-only" : "literal" });
}

function renderProvenance() {
  const path = state.inspecting;
  if (!path) return;
  const body = $("#provenance-body");
  const layer = layerOf(path);
  const info = state.provenance[path];
  const current = draftRaw(path);
  const original = savedRaw(path);

  $("#provenance-back").hidden = !state.inspectHistory.length;
  $("#provenance-layer").className = `layer-tag is-${layer}`;
  $("#provenance-layer").textContent = layer;
  $("#provenance-title").textContent = friendly(path.split(".").pop());
  $("#provenance-path").textContent = path;

  const value = state.readOnlySections.has(path.split(".")[0]) ? original : getPath(state.draft, path);
  const children = [el("div", { class: "provenance-value" }, valueChip(value), linkStatus(path, current, original),
    has(state.changes, path) ? el("span", { class: "changed-tag", text: "unsaved" }) : null)];

  if (!info) {
    children.push(el("p", { class: "provenance-empty", text: "No provenance data for this token." }));
    body.replaceChildren(...children);
    return;
  }

  const chain = [...info.chain];
  if (isReference(current) && current.slice(1) !== info.resolvesFrom) chain.splice(0, chain.length, current.slice(1));
  if (!isReference(current) && isReference(original)) chain.length = 0;
  const uses = downstream(path);
  const consumerFields = uses.reduce((total, use) => total + use.consumers.length, 0);
  const touched = new Set([layer, ...chain.map(layerOf)]);
  if (uses.some(use => use.recipe !== null)) touched.add("recipe");
  if (consumerFields) touched.add("consumer");

  children.push(el("ol", { class: "flow-strip", "aria-label": "Layers this token participates in" },
    ["foundation", "semantic", "recipe", "consumer"].map(name =>
      el("li", { class: touched.has(name) ? "is-on" : "", text: name }))));

  const lineage = [...chain].reverse().map(hop => hopItem(hop));
  lineage.push(hopItem(path, true));
  children.push(provenanceSection("Resolves from", chain.length ? plural(chain.length, "hop") : "literal root",
    el("div", { class: "hop-list" }, lineage)));

  const { referencedBy } = info;
  children.push(provenanceSection("Referenced by", referencedBy.length,
    referencedBy.length
      ? el("div", { class: "hop-list is-flat" }, referencedBy.map(hop => hopItem(hop)))
      : el("p", { class: "provenance-empty", text: "No tokens reference this one." })));

  const byRecipe = new Map();
  [...uses].sort((a, b) => (a.recipe === null) - (b.recipe === null)).forEach(use => {
    if (!byRecipe.has(use.recipe)) byRecipe.set(use.recipe, []);
    byRecipe.get(use.recipe).push(use);
  });
  const recipeCount = [...byRecipe.keys()].filter(recipe => recipe !== null).length;
  children.push(provenanceSection("Recipes → consumers", `${plural(recipeCount, "recipe")} · ${plural(consumerFields, "field")}`,
    byRecipe.size
      ? [...byRecipe].map(([recipe, entries]) => el("div", { class: "recipe-group" },
        el("div", { class: "recipe-name" },
          recipe === null
            ? [el("span", { class: "layer-tag", text: "adapter" }), el("strong", { text: "Direct mapping" })]
            : [el("span", { class: "layer-tag is-recipe", text: "recipe" }), el("strong", { text: recipe })]),
        entries.map(use => el("div", { class: "recipe-use" },
          recipe === null && !use.via ? null : el("div", { class: "recipe-field" },
            recipe === null ? null : el("code", { text: use.field ? `${recipe}.${use.field}` : recipe }),
            use.via ? el("span", { class: "via" }, "via ", tokenButton(use.via)) : null),
          el("ul", { class: "consumer-list" }, use.consumers.map(consumerItem))))))
      : el("p", { class: "provenance-empty", text: "No recipe or adapter consumes this token." })));

  body.replaceChildren(...children);
}

// Adapter manifest ---------------------------------------------------------

function renderAdapters() {
  const { targets } = state.manifest;
  const fieldCount = targets.reduce((total, item) => total + item.fields.length, 0);
  $("#adapter-summary").textContent = `${plural(targets.length, "target")} · ${plural(fieldCount, "field")}`;
  const families = new Set([...Object.keys(consumers), ...targets.map(item => item.family)]);
  $("#adapter-groups").replaceChildren(...[...families].flatMap(family => {
    const group = targets.filter(item => item.family === family);
    if (!group.length) return [];
    const [title, description] = consumers[family] || [family, ""];
    return el("section", { class: "adapter-group" },
      el("header", { class: "adapter-group-head" },
        el("div", {}, el("h3", { text: title }), description ? el("p", { text: description }) : null),
        el("code", { text: `${family} · ${plural(group.length, "target")}` })),
      group.map(adapterTarget));
  }));
}

function adapterTarget(item) {
  const { fields } = item;
  const head = el("div", { class: "adapter-target-head" },
    el("code", { class: "adapter-path", text: item.display, title: item.path }),
    item.mode ? el("span", { class: `mode-tag is-${item.mode}`, text: item.mode }) : null,
    el("span", { class: "field-count", text: plural(fields.length, "field") }));
  const dataset = item.mode ? { previewMode: item.mode } : {};

  if (!fields.length) {
    return el("article", { class: "adapter-target", dataset },
      head, el("p", { class: "adapter-note", text: emptyTargetNotes[item.family] || "No token fields mapped." }));
  }

  const article = el("details", { class: "adapter-target", dataset }, el("summary", {}, head));
  article.open = state.adapterOpen.get(item.path) ?? fields.length <= 30;
  article.addEventListener("toggle", () => state.adapterOpen.set(item.path, article.open));
  article.append(el("table", { class: "adapter-table" },
    el("thead", {}, el("tr", {}, ["Field", "Recipe", "Token", "Value"].map(label => el("th", { text: label })))),
    el("tbody", {}, fields.map(field => {
      const draft = field.token ? getPath(state.draft, field.token) : undefined;
      const pending = draft !== undefined && draft !== getPath(state.saved, field.token);
      return el("tr", { class: pending ? "is-pending" : "" },
        el("td", {}, el("code", { text: fieldLabel(field) || "—" })),
        el("td", {}, field.recipe ? el("code", { class: "recipe-ref", text: field.recipeField ? `${field.recipe}.${field.recipeField}` : field.recipe }) : el("span", { class: "muted", text: "—" })),
        el("td", {}, field.token ? tokenButton(field.token) : el("span", { class: "muted", text: "—" })),
        el("td", { title: pending ? `Unsaved draft; generated value is ${displayValue(field.value)}` : null }, valueChip(pending ? draft : field.value ?? draft)));
    }))));
  return article;
}

async function copyManifest() {
  try {
    await navigator.clipboard.writeText(JSON.stringify(state.manifest, null, 2));
    showNotice(`Adapter manifest copied (${plural(state.manifest.targets.length, "target")}).`);
  } catch (error) {
    showNotice(`Could not copy the manifest: ${error.message}`, true);
  }
}

function selectView(view) {
  state.view = view;
  $$(".tab").forEach(tab => tab.classList.toggle("is-active", tab.dataset.view === view));
  $$(".view").forEach(panel => panel.classList.toggle("is-active", panel.id === `${view}-view`));
  $("#view-title").textContent = descriptions[view][0];
  $("#view-description").textContent = descriptions[view][1];
  history.replaceState(null, "", `#${view}`);
  if (view === "adapters" && state.draft) renderAdapters();
  // captures.js loads after this file; it renders an initial #captures itself.
  if (view === "captures" && typeof renderCaptures === "function") renderCaptures();
}

function bind() {
  $$(".tab").forEach(tab => tab.addEventListener("click", () => selectView(tab.dataset.view)));
  $$(".mode-toggle button").forEach(button => button.addEventListener("click", () => {
    state.mode = button.dataset.mode;
    document.body.dataset.mode = state.mode;
    $$(".mode-toggle button").forEach(candidate => candidate.classList.toggle("is-active", candidate === button));
  }));
  $("#token-search").addEventListener("input", renderInspector);
  $("#reset").addEventListener("click", reset);
  $("#save").addEventListener("click", save);
  $("#apply").addEventListener("click", apply);
  $("#copy-manifest").addEventListener("click", copyManifest);
  $("#provenance-close").addEventListener("click", closeProvenance);
  $("#provenance-back").addEventListener("click", () => {
    const previous = state.inspectHistory.pop();
    if (!previous) return;
    state.inspecting = previous;
    renderProvenance();
    markInspecting();
    jumpToToken(previous);
  });
  $("#issue-summary").addEventListener("click", () => {
    const first = currentIssues().find(issue => issue.path && state.paths.has(issue.path));
    if (first) jumpToToken(first.path);
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && state.inspecting) closeProvenance();
  });
  window.addEventListener("beforeunload", event => {
    if (hasChanges()) event.preventDefault();
  });
}

bind();
fillSpecimens();
loadTerminalFixture();
const initialView = location.hash.slice(1);
if (descriptions[initialView]) selectView(initialView);
document.body.dataset.mode = state.mode;
// captures.js waits for this before choosing between api/captures and api/captures.json.
const stateLoaded = load();
