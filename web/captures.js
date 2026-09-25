// Native capture view: live specimens beside grim captures of the real shell.
// Relies on studio.js globals: state, stateLoaded, $, el, setVariables, request, showNotice.
// A static snapshot reads api/captures.json and cannot run captures.

const captureState = { data: null, running: null };

const captureStatusText = {
  supported: "capturable",
  "needs-fixture": "needs fixture",
  manual: "manual",
};

function captureFor(surface, mode) {
  return captureState.data?.captures.find(item => item.surface === surface && item.mode === mode) || null;
}

function formatCaptureDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function shortCommit(value) {
  return value && /^[0-9a-f]{40}$/.test(value) ? value.slice(0, 10) : value || "—";
}

const windowSurfaces = new Set(["windows", "terminal", "browser"]);

// Window surfaces clone the tiled desktop specimen, or one window tile from it. The desktop is
// drawn at ½ (1280 CSS px for 2560 logical px); a lone tile keeps that scale with `zoom: .5`
// and takes its logical size from the native capture (the Windows view may be hidden).
function windowSpecimen(surface, mode) {
  const stage = el("div", { class: `window-stage capture-stage ${mode}-preview is-${surface.name}` });
  const source = document.querySelector(surface.selector.replaceAll("{mode}", mode));
  if (!source) {
    stage.append(el("p", { class: "capture-empty", text: "Open the Windows view once to render this specimen." }));
  } else if (surface.name === "windows") {
    const copy = source.cloneNode(true);
    copy.removeAttribute("id");
    stage.append(copy);
  } else {
    const box = captureFor(surface.name, mode)?.box;
    const copy = source.cloneNode(true);
    copy.style.width = `${box?.width || source.offsetWidth * 2 || 1266}px`;
    copy.style.height = `${box?.height || source.offsetHeight * 2 || 1395}px`;
    stage.append(el("div", { class: "capture-window-tile" }, copy));
  }
  if (state.draft) setVariables(stage, mode);
  return stage;
}

function liveSpecimen(surface, mode) {
  if (surface.selector && windowSurfaces.has(surface.name)) return windowSpecimen(surface, mode);
  const stage = el("div", { class: `desktop capture-stage ${mode}-preview is-${surface.name}` });
  const source = document.querySelector(`#shell-view .${mode}-preview ${surface.specimen}`)
    || document.querySelector(`#hunk-view .${mode}-preview${surface.specimen}`)
    || document.querySelector(`.${mode}-preview ${surface.specimen}`);
  if (source) {
    const copy = source.cloneNode(true);
    copy.removeAttribute("id");
    stage.append(copy);
  } else {
    stage.append(el("p", { class: "capture-empty", text: "No live specimen for this surface yet." }));
  }
  if (state.draft) setVariables(stage, mode);
  return stage;
}

// The API serves images from /captures/; resolve them against the page so a subpath export works.
const relative = url => url.replace(/^\//, "");

// The capture's logical width (its pixels at the capture scale).
function logicalWidth(capture) {
  return capture.pixels && capture.scale ? `${capture.pixels.width / capture.scale}px` : null;
}

function nativeImage(capture, label) {
  const image = el("img", { src: relative(capture.imageUrl), alt: label, class: "capture-image", loading: "lazy" });
  image.style.width = logicalWidth(capture) ?? "";
  return image;
}

function comparisonSlider(capture) {
  const top = el("img", { src: relative(capture.browserUrl), alt: "Browser specimen", class: "slider-browser" });
  const base = el("img", { src: relative(capture.imageUrl), alt: "Native capture", class: "slider-native" });
  const frame = el("div", { class: "slider-frame" }, base, top, el("span", { class: "slider-handle", "aria-hidden": "true" }));
  frame.style.width = logicalWidth(capture) ?? "100%";
  const input = el("input", { type: "range", min: "0", max: "100", value: "50", "aria-label": "Reveal native capture" });
  const update = () => frame.style.setProperty("--reveal", `${input.value}%`);
  input.addEventListener("input", update);
  update();
  const result = capture.comparison?.result;
  return el("div", { class: "capture-slider" },
    el("div", { class: "slider-labels" }, el("span", { text: "Browser" }), el("span", { text: "Native" })),
    el("div", { class: "slider-scroll" }, frame),
    input,
    result ? el("p", { class: "capture-metric", text: `RMSE ${result.normalized.toFixed(4)} (${result.absolute.toFixed(0)}) — ${result.note}` }) : null);
}

function describeValue(value) {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value.colors ? `${value.colors.join(" ")} ${value.angle || 0}deg` : Object.values(value).join(" ");
  }
  return Array.isArray(value) ? value.join(",") : String(value);
}

function windowRows(capture) {
  const hypr = capture.hyprland || {};
  const ghostty = capture.ghostty || {};
  const effective = ghostty.effective || {};
  const browser = capture.browser || {};
  const chrome = browser.chrome || {};
  const page = browser.page || {};
  const primaryFont = (ghostty.fonts || [])[0] || {};
  const delta = Object.entries(capture.theme_delta || {}).map(([name, value]) => `${name}=${describeValue(value)}`);
  const geometry = Object.entries(capture.windows || {}).map(([role, item]) =>
    `${role} ${item.box.x},${item.box.y} ${item.box.width}×${item.box.height}`);
  const families = [].concat(effective["font-family"] || []);
  return [
    ["Output", capture.output ? `${capture.output.name}${capture.output.headless ? " (headless)" : ""} · workspace ${capture.output.workspace}` : "—"],
    ["Windows", geometry.join(" · ") || "—"],
    ["Border", `${hypr["general:border_size"] ?? "—"}px · active ${describeValue(hypr["general:col.active_border"])} · inactive ${describeValue(hypr["general:col.inactive_border"])}`],
    ["Gaps", `in ${describeValue(hypr["general:gaps_in"])} · out ${describeValue(hypr["general:gaps_out"])}`],
    ["Rounding", `${hypr["decoration:rounding"] ?? "—"} (power ${hypr["decoration:rounding_power"] ?? "—"})`],
    ["Shadow", `range ${hypr["decoration:shadow:range"] ?? "—"} · offset ${describeValue(hypr["decoration:shadow:offset"])} · ${describeValue(hypr["decoration:shadow:color"])} / ${describeValue(hypr["decoration:shadow:color_inactive"])}`],
    ["Mode overrides", delta.length ? delta.join(" · ") : "none (installed theme)"],
    ["Ghostty", `${ghostty.version || "—"} · ${families.join(", ") || "—"} ${effective["font-size"] || ""}pt · padding ${effective["window-padding-x"] || "—"}/${effective["window-padding-y"] || "—"} · cell height ${effective["adjust-cell-height"] || "default"}`],
    ["Terminal font", primaryFont.family ? `${primaryFont.family} ${primaryFont.style || ""}`.trim() : "—", primaryFont.fallback ? "is-warning" : ""],
    ["Browser", `${browser.name || "—"} ${browser.package ? browser.package.split(" ")[1] : ""} (${browser.version || "—"})`],
    ["Page", `${page.source || "—"} · ${page.url || "—"}`, page.source === "placeholder" ? "is-note" : ""],
    ["Chrome color", `${chrome.theme_color || "—"} from machine policy · does not follow the mode`, "is-note"],
  ];
}

function metadataCard(capture) {
  const versions = capture.versions || {};
  const theme = capture.theme || {};
  const font = capture.font || {};
  const dotfiles = capture.dotfiles || {};
  const rows = [
    ["Captured", formatCaptureDate(capture.date)],
    ["Box", capture.box ? `${capture.box.x},${capture.box.y} ${capture.box.width}×${capture.box.height} @ ${capture.scale}×` : "—"],
    ["Pixels", capture.pixels ? `${capture.pixels.width}×${capture.pixels.height} on ${capture.monitor}` : "—"],
    ["Applied theme", (theme.applied || "—").split("/").pop()],
    ["User theme", theme.user_theme || "—"],
    ["Qt", versions["qt6-base"] || "—"],
    ["Quickshell", versions.quickshell || "—"],
    ["Hyprland", versions.hyprland || "—"],
    ["Dotfiles", `${shortCommit(dotfiles.commit)}${dotfiles.dirty ? " (dirty)" : ""}`],
    ["Studio", capture.studio?.commit === "uncommitted"
      ? `uncommitted · tokens ${String(capture.studio.tokens_sha256 || "").slice(0, 10)}`
      : shortCommit(capture.studio?.commit)],
    ["Bar font", font.family ? `${font.family} ${font.style || ""}`.trim() : "—", font.fallback ? "is-warning" : ""],
    ["Backdrop", capture.backdrop?.length ? capture.backdrop.join(", ") : capture.live?.length ? `live: ${capture.live.join(", ")}` : "—"],
    ...(capture.windows ? windowRows(capture) : []),
  ];
  const restore = capture.restore?.length
    ? el("details", { class: "capture-restore" },
      el("summary", { text: `Restore steps (${capture.restore.length})` }),
      el("ol", {}, capture.restore.map(step => el("li", { text: step }))))
    : null;
  return [el("dl", { class: "capture-meta" },
    rows.map(([term, value, className]) => [
      el("dt", { text: term }),
      el("dd", { class: className || null, text: value, title: term === "Bar font" && font.fallback ? `fontconfig resolved "${font.requested}" to a fallback` : null }),
    ])), restore];
}

function capturePair(surface, mode) {
  const capture = captureFor(surface.name, mode);
  const native = capture?.imageUrl
    ? nativeImage(capture, `${surface.label} ${mode} native capture`)
    : el("div", { class: "capture-placeholder" },
      el("strong", { text: "Not captured yet" }),
      el("span", { text: `script/capture run --surface ${surface.name} --mode ${mode}` }));
  return el("article", { class: "capture-pair", dataset: { previewMode: mode } },
    el("div", { class: "frame-label" },
      el("span", { text: mode === "light" ? "Light" : "Dark" }),
      el("span", { text: capture ? `native ${capture.pixels?.width}×${capture.pixels?.height}` : "no native capture" })),
    el("div", { class: "capture-columns" },
      el("figure", {}, el("figcaption", { text: "Live specimen" }), liveSpecimen(surface, mode)),
      el("figure", {}, el("figcaption", { text: "Native (grim)" }), el("div", { class: "capture-native" }, native))),
    capture?.browserUrl && capture?.imageUrl ? comparisonSlider(capture) : null,
    capture ? metadataCard(capture) : null,
    capture?.restore_problems?.length
      ? el("p", { class: "capture-warning", text: `Restore problems: ${capture.restore_problems.join("; ")}` }) : null);
}

async function runCapture(surface, button) {
  if (captureState.running) return;
  captureState.running = surface.name;
  button.disabled = true;
  button.classList.add("is-running");
  button.textContent = "Capturing…";
  const mode = state.mode;
  showNotice(`Capturing ${surface.label} (${mode}). The fixture appears briefly on your screen.`);
  try {
    captureState.data = await request("api/captures/run", { surface: surface.name, mode });
    showNotice(`Captured ${surface.label} (${mode}).`);
  } catch (error) {
    showNotice(error.message, true);
    await loadCaptures();
  } finally {
    captureState.running = null;
    drawCaptures();
  }
}

function surfaceCard(surface) {
  const capturable = surface.status === "supported";
  let button = null;
  if (capturable && !state.snapshot) {
    const running = captureState.running === surface.name;
    button = el("button", {
      class: `button capture-button${running ? " is-running" : ""}`, type: "button", text: running ? "Capturing…" : "Capture",
      disabled: Boolean(captureState.running), onclick: () => runCapture(surface, button),
    });
  }
  const head = el("div", { class: "capture-head" },
    el("div", {},
      el("h3", { text: surface.label }),
      el("span", { class: `capture-status is-${surface.status}`, text: captureStatusText[surface.status] || surface.status }),
      capturable ? el("span", { class: `capture-status ${surface.deterministic ? "is-deterministic" : "is-live"}`, text: surface.deterministic ? "deterministic" : "live content" }) : null),
    button);
  const notes = [];
  if (surface.method) notes.push(el("p", { class: "capture-method", text: surface.method }));
  if (surface.reason) notes.push(el("p", { class: capturable ? "capture-note" : "capture-reason", text: surface.reason }));
  const body = capturable
    ? el("div", { class: "capture-modes" }, ["light", "dark"].map(mode => capturePair(surface, mode)))
    : null;
  return el("section", { class: `capture-surface is-${surface.status}` }, head, notes, body);
}

function drawCaptures() {
  if (!captureState.data) return;
  const { surfaces, captures } = captureState.data;
  const capturable = surfaces.filter(surface => surface.status === "supported").length;
  $("#capture-summary").textContent =
    `${captures.length} native capture${captures.length === 1 ? "" : "s"} · ${capturable} capturable surfaces · ${surfaces.length - capturable} awaiting fixtures`;
  $("#capture-groups").replaceChildren(...surfaces.map(surfaceCard));
}

async function loadCaptures() {
  await stateLoaded;
  try {
    captureState.data = await request(state.snapshot ? "api/captures.json" : "api/captures");
  } catch (error) {
    $("#capture-summary").textContent = `Captures unavailable: ${error.message}`;
    return;
  }
  drawCaptures();
}

function renderCaptures() {
  if (!captureState.running) loadCaptures();
}

if (state.view === "captures") renderCaptures();
