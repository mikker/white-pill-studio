// Change report: shown before Save & generate or Apply writes anything.
//
// WhitePillReport.confirm(changes, action) fetches POST api/report and resolves
// true to proceed or false to cancel. With no changes it resolves true without
// showing anything. Uses studio.js globals: el, plural, fieldLabel, request, showNotice.

(() => {
  const HEX = /^#[0-9a-f]{6}$/i;
  const actions = {
    save: { label: "Save & generate", verb: "saving" },
    apply: { label: "Apply", verb: "applying" },
  };

  function text(value) {
    if (value === null || value === undefined) return "∅";
    if (typeof value === "string") return value;
    return JSON.stringify(value);
  }

  // A value chip; a per-mode object (recipe fields) becomes one chip per mode.
  function chip(value, className = "") {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return el("span", { class: "report-modes" },
        Object.entries(value).map(([mode, item]) =>
          el("span", { class: "report-mode" }, el("small", { text: mode }), chip(item, className))));
    }
    const shown = text(value);
    return el("span", { class: `report-chip ${className}`.trim(), title: shown },
      HEX.test(shown) ? el("i", { class: "report-swatch", style: `background:${shown}` }) : null,
      el("code", { text: shown }));
  }

  function change(before, after) {
    return el("span", { class: "report-change" }, chip(before, "is-old"), el("span", { class: "report-arrow", text: "→" }), chip(after, "is-new"));
  }

  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

  function tokenRow(item) {
    const resolvedDiffers = !same(item.oldRaw, item.oldValue) || !same(item.newRaw, item.newValue);
    return el("li", { class: "report-token" },
      el("code", { class: "report-path", text: item.path, title: item.path }),
      change(item.oldRaw, item.newRaw),
      resolvedDiffers ? el("span", { class: "report-resolved" }, el("small", { text: "resolves" }), change(item.oldValue, item.newValue)) : null);
  }

  function targetDetails(target) {
    return el("details", { class: "report-target" },
      el("summary", {},
        el("code", { class: "report-file", text: target.display, title: target.path }),
        target.mode ? el("span", { class: `report-mode-tag is-${target.mode}`, text: target.mode }) : null,
        el("span", { class: "report-count", text: plural(target.fields.length, "field") })),
      el("table", { class: "report-fields" },
        el("tbody", {}, target.fields.map(field =>
          el("tr", {},
            el("td", {}, el("code", { text: fieldLabel(field) || "—", title: field.token ? `from ${field.token}` : null })),
            el("td", {}, change(field.old, field.new)))))));
  }

  function issueList(issues) {
    if (!issues.length) return null;
    const errors = issues.filter(issue => issue.level === "error").length;
    return el("section", { class: `report-section report-issues${errors ? " has-errors" : ""}` },
      el("h3", {}, "Validation", el("span", { text: `${plural(errors, "error")} · ${plural(issues.length - errors, "warning")}` })),
      el("ul", {}, issues.map(issue =>
        el("li", { class: `is-${issue.level}` },
          el("b", { text: issue.level }),
          issue.path ? el("code", { text: issue.path }) : null,
          el("span", { text: issue.message })))));
  }

  function render(report, action) {
    const { totals } = report;
    const errors = report.issues.filter(issue => issue.level === "error").length;
    const repositories = report.consumers.filter(consumer => consumer.targets > 0);
    const groups = repositories.map(consumer =>
      el("div", { class: "report-consumer" },
        el("div", { class: "report-consumer-head" },
          el("strong", { text: consumer.name }),
          el("code", { text: consumer.root, title: consumer.root }),
          el("span", { text: `${plural(consumer.fields, "field")} · ${plural(consumer.targets, "file")}` })),
        report.targets.filter(target => target.consumer === consumer.name).map(targetDetails)));

    const cancel = el("button", { class: "button", type: "button", "data-report": "cancel", text: "Cancel" });
    const confirm = el("button", {
      class: "button button-primary", type: "button", "data-report": "confirm", text: actions[action].label,
      disabled: errors > 0, title: errors ? `Fix ${plural(errors, "validation error")} before ${actions[action].verb}` : null,
    });
    const dialog = el("div", { class: "report-dialog", role: "dialog", "aria-modal": "true", "aria-labelledby": "report-title" },
      el("header", { class: "report-head" },
        el("div", {},
          el("h2", { id: "report-title", text: "Review changes" }),
          el("p", { text: `${plural(totals.tokens, "token")} → ${plural(totals.fields, "field")} in ${plural(totals.targets, "file")}` +
            (repositories.length ? ` across ${repositories.length} ${repositories.length === 1 ? "repository" : "repositories"}` : "") })),
        el("button", { class: "icon-button", type: "button", "data-report": "cancel", "aria-label": "Cancel", title: "Cancel (Esc)", text: "×" })),
      el("div", { class: "report-body" },
        issueList(report.issues),
        el("section", { class: "report-section" },
          el("h3", {}, "Tokens", el("span", { text: plural(totals.tokens, "change") })),
          el("ul", { class: "report-tokens" }, report.tokens.map(tokenRow))),
        el("section", { class: "report-section" },
          el("h3", {}, "Generated files", el("span", { text: `${plural(totals.targets, "file")} · ${plural(totals.fields, "field")}` })),
          groups.length ? groups : el("p", { class: "report-empty", text: "No generated consumer field changes. Only the canonical TOML changes." }))),
      el("footer", { class: "report-foot" }, cancel, confirm));
    return { dialog, confirm };
  }

  function show(report, action) {
    const host = document.getElementById("change-report");
    const previous = document.activeElement;
    const { dialog, confirm } = render(report, action);
    host.replaceChildren(dialog);
    host.hidden = false;
    return new Promise(resolve => {
      const close = result => {
        document.removeEventListener("keydown", onKey, true);
        host.hidden = true;
        host.replaceChildren();
        previous?.focus?.();
        resolve(result);
      };
      const onKey = event => {
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopImmediatePropagation();
          close(false);
        }
      };
      document.addEventListener("keydown", onKey, true);
      host.onclick = event => {
        if (event.target === host) return close(false);
        const role = event.target.closest("[data-report]")?.dataset.report;
        if (role === "cancel") close(false);
        else if (role === "confirm" && !confirm.disabled) close(true);
      };
      (confirm.disabled ? dialog.querySelector("[data-report=cancel]") : confirm).focus();
    });
  }

  async function confirmChanges(changes, action) {
    if (!Object.keys(changes).length) return true;
    let report;
    try {
      ({ report } = await request("api/report", { changes }));
    } catch (error) {
      showNotice(error.message, true);
      return false;
    }
    return report.totals.tokens === 0 || show(report, action);
  }

  window.WhitePillReport = { confirm: confirmChanges };
})();
