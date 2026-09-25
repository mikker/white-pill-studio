"""Change reports: what a token revision does to every generated consumer file.

`change_report` is pure: it compares the saved token document with a draft
(both raw trees) and describes the changed tokens and every consumer field
whose generated value would change. Nothing is written.
"""

from __future__ import annotations

import json

import adapters
import tokens_model
from tokens_model import ReferenceProblem, Resolver, is_recipe_path


def resolved_value(resolver: Resolver, path: str):
    """A token's resolved value; for a recipe field, per mode (collapsed when equal).

    Returns None when the token does not exist or cannot be resolved.
    """
    if path not in resolver.flat:
        return None
    if not is_recipe_path(path):
        try:
            return resolver.value(path)
        except ReferenceProblem:
            return None
    values = {}
    for mode, lookup in resolver.recipe_lookups(path):
        try:
            values[mode] = resolver.value(lookup, mode)
        except ReferenceProblem:
            values[mode] = None
    distinct = {json.dumps(value, sort_keys=True) for value in values.values()}
    if len(distinct) == 1:
        return next(iter(values.values()))
    return values or None


def token_changes(saved: dict, draft: dict) -> list:
    """Every scalar token whose raw value differs, with raw and resolved values."""
    before, after = Resolver(saved), Resolver(draft)
    paths = list(after.flat) + [path for path in before.flat if path not in after.flat]
    result = []
    for path in paths:
        old_raw, new_raw = before.flat.get(path), after.flat.get(path)
        if old_raw == new_raw and type(old_raw) is type(new_raw):
            continue
        result.append({
            "path": path,
            "oldRaw": old_raw, "newRaw": new_raw,
            "oldValue": resolved_value(before, path), "newValue": resolved_value(after, path),
        })
    return result


def field_diffs(old_fields: list, new_fields: list) -> list:
    old = {(item["section"], item["key"]): item for item in old_fields}
    new = {(item["section"], item["key"]): item for item in new_fields}
    keys = list(new) + [key for key in old if key not in new]
    result = []
    for key in keys:
        before, after = old.get(key, {}), new.get(key, {})
        old_value, new_value = before.get("value"), after.get("value")
        if key in old and key in new and old_value == new_value and type(old_value) is type(new_value):
            continue
        source = after or before
        result.append({
            "section": key[0], "key": key[1], "old": old_value, "new": new_value,
            "token": source.get("token"), "recipe": source.get("recipe"), "recipeField": source.get("recipeField"),
        })
    return result


def change_report(saved: dict, draft: dict, consumers: list) -> dict:
    """Describe the effect of replacing `saved` with `draft` on every consumer.

    Returns `{tokens, targets, consumers, totals, issues}`: changed tokens
    (raw and resolved, old → new), each generated target with at least one
    changed field (grouped by consumer repository, in generation order), a
    per-consumer summary, totals, and the draft's validation issues.
    """
    before, after = Resolver(saved), Resolver(draft)
    targets = []
    for target in adapters.targets(consumers):
        fields = field_diffs(adapters.target_fields(before, target), adapters.target_fields(after, target))
        if fields:
            targets.append({
                "consumer": target.consumer, "root": str(target.root), "display": target.display,
                "path": str(target.path), "family": target.family, "mode": target.mode, "fields": fields,
            })
    summary = []
    for consumer in consumers:
        own = [item for item in targets if item["consumer"] == consumer.name]
        summary.append({
            "name": consumer.name, "root": str(consumer.root),
            "targets": len(own), "fields": sum(len(item["fields"]) for item in own),
        })
    tokens = token_changes(saved, draft)
    return {
        "tokens": tokens,
        "targets": targets,
        "consumers": summary,
        "totals": {
            "tokens": len(tokens), "targets": len(targets),
            "fields": sum(len(item["fields"]) for item in targets),
        },
        "issues": tokens_model.validate(draft),
    }


# ── Text form ────────────────────────────────────────────────────────────


def show(value) -> str:
    if value is None:
        return "∅"
    if isinstance(value, dict):
        return " · ".join(f"{mode} {show(item)}" for mode, item in value.items())
    if isinstance(value, str):
        return value
    return json.dumps(value)


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def format_report(report: dict) -> str:
    totals = report["totals"]
    lines = [
        f"Change report: {plural(totals['tokens'], 'token')} → "
        f"{plural(totals['fields'], 'field')} in {plural(totals['targets'], 'file')}",
    ]
    if report["tokens"]:
        lines += ["", "Tokens"]
        width = max(len(item["path"]) for item in report["tokens"])
        for item in report["tokens"]:
            line = f"  {item['path']:<{width}}  {show(item['oldRaw'])} → {show(item['newRaw'])}"
            if (item["oldValue"], item["newValue"]) != (item["oldRaw"], item["newRaw"]):
                line += f"   (resolved {show(item['oldValue'])} → {show(item['newValue'])})"
            lines.append(line)
    for consumer in report["consumers"]:
        own = [item for item in report["targets"] if item["consumer"] == consumer["name"]]
        if not own:
            continue
        lines += ["", f"{consumer['name']}  {consumer['root']}  "
                      f"({plural(consumer['fields'], 'field')} in {plural(consumer['targets'], 'file')})"]
        for target in own:
            lines.append(f"  {target['display']}")
            for field in target["fields"]:
                label = ".".join(part for part in (field["section"], field["key"]) if part)
                lines.append(f"    {label}: {show(field['old'])} → {show(field['new'])}")
    issues = report["issues"]
    if issues:
        errors = len(tokens_model.errors(issues))
        lines += ["", f"Validation: {plural(errors, 'error')}, {plural(len(issues) - errors, 'warning')}"]
        lines += [f"  {tokens_model.format_issue(item)}" for item in issues]
    return "\n".join(lines)


def parse_assignment(text: str) -> tuple:
    """`path=value` → (path, value); the value is JSON when it parses, else a string."""
    path, separator, value = text.partition("=")
    if not separator or not path.strip():
        raise ValueError(f"Expected path=value, not {text!r}")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    if isinstance(parsed, (dict, list)) or parsed is None:
        parsed = value
    return path.strip(), parsed
