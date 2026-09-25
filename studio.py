#!/usr/bin/env python3
"""Local design-token studio backed by a canonical TOML source file."""

from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import tomllib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import adapters  # noqa: E402
import captures  # noqa: E402
import report  # noqa: E402
import tokens_model  # noqa: E402
from tokens_model import MODES, ReferenceProblem, Resolver, is_recipe_path, is_reference, project_root  # noqa: E402

WEB_ROOT = ROOT / "web"
DEFAULT_SOURCE = ROOT / "design-system" / "tokens.toml"
SECTION_RE = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")
KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_-]+)(\s*=\s*)(.*)$")


class StudioError(Exception):
    pass


def read_tokens(source: pathlib.Path) -> dict:
    try:
        return tokens_model.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise StudioError(f"Unable to read {source}: {error}") from error


def value_at(tokens: dict, path: str):
    try:
        value = tokens_model.node_at(tokens, path)
    except KeyError:
        raise StudioError(f"Unknown token: {path}") from None
    if isinstance(value, dict):
        raise StudioError(f"Token is not a scalar: {path}")
    return value


def compatible_value(original, replacement) -> bool:
    if isinstance(original, bool):
        return isinstance(replacement, bool)
    if isinstance(original, int):
        return isinstance(replacement, int) and not isinstance(replacement, bool)
    if isinstance(original, float):
        return isinstance(replacement, (int, float)) and not isinstance(replacement, bool)
    return isinstance(original, str) and isinstance(replacement, str)


def split_toml_comment(text: str) -> tuple[str, str]:
    quote = None
    escaped = False
    for index, character in enumerate(text):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote == '"':
            escaped = True
            continue
        if character in {'"', "'"}:
            quote = None if quote == character else character if quote is None else quote
            continue
        if character == "#" and quote is None:
            value = text[:index].rstrip()
            spacing = text[len(value):index]
            return value, spacing + text[index:]
    return text.rstrip(), text[len(text.rstrip()):]


def resolved_original(resolver: Resolver, path: str):
    """The value a token currently resolves to (first resolvable mode for recipes)."""
    for mode in (MODES if is_recipe_path(path) else (None,)):
        try:
            return resolver.value(path, mode)
        except ReferenceProblem:
            continue
    return None


def set_value(tokens: dict, path: str, value) -> None:
    parent, _, key = path.rpartition(".")
    tokens_model.node_at(tokens, parent)[key] = value


def prepare_changes(tokens: dict, changes: dict[str, object]) -> tuple[dict, dict]:
    """Check paths and value types; return (validated changes, changed document).

    A literal replaces the token's value, unlinking it if it was a reference.
    A string starting with "@" links the token to another token; its resolved
    type must be compatible with what the token resolves to today. Unknown
    targets and cycles are left to validation so they surface as issues.
    """
    if not isinstance(changes, dict):
        raise StudioError("Changes must be an object")
    resolver = Resolver(tokens)
    changed = copy.deepcopy(tokens)
    validated = {}
    for path, replacement in changes.items():
        if not isinstance(path, str) or "." not in path:
            raise StudioError(f"Invalid token path: {path!r}")
        value_at(tokens, path)
        original = resolved_original(resolver, path)
        if isinstance(replacement, (dict, list)) or replacement is None:
            raise StudioError(f"Wrong value type for {path}: expected a scalar")
        if not is_reference(replacement) and original is not None and not compatible_value(original, replacement):
            raise StudioError(
                f"Wrong value type for {path}: expected {type(original).__name__}"
            )
        set_value(changed, path, replacement)
        validated[path] = replacement

    new_resolver = Resolver(changed)
    for path, replacement in validated.items():
        if not is_reference(replacement):
            continue
        linked = resolved_original(new_resolver, path)
        original = resolved_original(resolver, path)
        if linked is not None and original is not None and not compatible_value(original, linked):
            raise StudioError(
                f"Wrong value type for {path}: {replacement} resolves to {type(linked).__name__}, "
                f"expected {type(original).__name__}"
            )
    return validated, changed


def reference_errors(tokens: dict) -> list:
    return [
        item for item in tokens_model.validate(tokens)
        if item["code"] == "reference" and item["level"] == "error"
    ]


def update_source(source: pathlib.Path, changes: dict[str, object]) -> None:
    tokens = read_tokens(source)
    validated, changed = prepare_changes(tokens, changes)
    existing = {item["message"] for item in reference_errors(tokens)}
    problems = [item for item in reference_errors(changed) if item["message"] not in existing]
    if problems:
        raise StudioError("; ".join(item["message"] for item in problems))

    lines = source.read_text().splitlines(keepends=True)
    current_section = ""
    remaining = set(validated)
    output = []

    for line in lines:
        section_match = SECTION_RE.match(line.rstrip("\n"))
        if section_match:
            current_section = section_match.group(1)
            output.append(line)
            continue

        key_match = KEY_RE.match(line.rstrip("\n"))
        if not key_match:
            output.append(line)
            continue

        key = key_match.group(2)
        path = f"{current_section}.{key}" if current_section else key
        if path not in validated:
            output.append(line)
            continue

        newline = "\n" if line.endswith("\n") else ""
        _, suffix = split_toml_comment(key_match.group(4))
        output.append(
            f"{key_match.group(1)}{key}{key_match.group(3)}"
            f"{adapters.toml_scalar(validated[path])}{suffix}{newline}"
        )
        remaining.remove(path)

    if remaining:
        raise StudioError(f"Could not locate tokens: {', '.join(sorted(remaining))}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{source.name}.", dir=source.parent, text=True
    )
    try:
        with os.fdopen(descriptor, "w") as temporary:
            temporary.writelines(output)
        os.replace(temporary_name, source)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def command_result(command: list[str], cwd: pathlib.Path) -> dict:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise StudioError(f"Could not run {' '.join(command)}: {error}") from error

    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if result.returncode:
        raise StudioError(output or f"Command failed with status {result.returncode}")
    return {"command": " ".join(command), "output": output}


def generate(source: pathlib.Path) -> dict:
    root = project_root(source)
    generator = root / "script" / "design-system"
    if not generator.is_file():
        raise StudioError(f"No generator found at {generator}")
    return command_result([str(generator), "generate"], root)


def git_status(source: pathlib.Path) -> str:
    root = project_root(source)
    try:
        result = subprocess.run(
            ["git", "status", "--short", "--", str(source.relative_to(root))],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
        return result.stdout.strip()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""


def consumers(source: pathlib.Path) -> list:
    """Consumer repositories from design-system/consumers.toml (see adapters.load_consumers)."""
    try:
        return adapters.load_consumers(project_root(source))
    except adapters.ConsumerError as error:
        raise StudioError(str(error)) from error


def change_report(source: pathlib.Path, changes: dict) -> dict:
    """What saving `changes` would regenerate, without writing anything."""
    tokens = read_tokens(source)
    _, changed = prepare_changes(tokens, changes)
    return report.change_report(tokens, changed, consumers(source))


def studio_state(source: pathlib.Path) -> dict:
    """The GET /api/state payload (also written by `script/design-system export`)."""
    raw = read_tokens(source)
    manifest = adapters.manifest(raw, project_root(source), consumers(source))
    return {
        "ok": True,
        "source": str(source),
        "sourceStatus": git_status(source),
        "tokens": tokens_model.resolved_tree(raw),
        "raw": raw,
        "readOnlySections": list(tokens_model.READ_ONLY_SECTIONS),
        "provenance": adapters.provenance(raw, manifest),
        "manifest": manifest,
        "issues": tokens_model.validate(raw),
    }


class StudioHandler(BaseHTTPRequestHandler):
    server_version = "WhitePillStudio/0.1"

    @property
    def source(self) -> pathlib.Path:
        return self.server.source  # type: ignore[attr-defined]

    def log_message(self, format_string: str, *args) -> None:
        print(f"{self.address_string()} — {format_string % args}")

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, error: Exception) -> None:
        self.send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            try:
                self.send_json(studio_state(self.source))
            except StudioError as error:
                self.send_error_json(error)
            return

        if captures.handle_get(self, path):
            return

        requested = "index.html" if path == "/" else unquote(path.lstrip("/"))
        file_path = (WEB_ROOT / requested).resolve()
        if WEB_ROOT not in file_path.parents or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        body = file_path.read_bytes()
        mime, _ = mimetypes.guess_type(file_path)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def read_payload(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                raise StudioError("Request is too large")
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as error:
            raise StudioError("Invalid JSON request") from error
        if not isinstance(payload, dict):
            raise StudioError("Request must be a JSON object")
        return payload

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self.read_payload()
            if captures.handle_post(self, path, payload):
                return

            # prepare_changes rejects a non-object `changes`.
            if path == "/api/report":
                self.send_json({"ok": True, "report": change_report(self.source, payload.get("changes", {}))})
                return

            if path == "/api/validate":
                _, changed = prepare_changes(read_tokens(self.source), payload.get("changes", {}))
                self.send_json({"ok": True, "issues": tokens_model.validate(changed)})
                return

            if path == "/api/save":
                changes = payload.get("changes", {})
                if not isinstance(changes, dict) or not changes:
                    raise StudioError("No token changes supplied")
                _, changed = prepare_changes(read_tokens(self.source), changes)
                issues = tokens_model.validate(changed)
                if tokens_model.errors(issues):
                    self.send_json(
                        {"ok": False, "error": "Validation failed", "issues": issues},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                update_source(self.source, changes)
                result = generate(self.source)
                self.send_json({**studio_state(self.source), "result": result})
                return

            if path == "/api/generate":
                self.send_json({**studio_state(self.source), "result": generate(self.source)})
                return

            if path == "/api/apply":
                # Omarchy reads a staged copy of the theme under
                # ~/.local/state/omarchy/current/theme, so regenerated fragments
                # only reach the shell, Hyprland, terminals and browser policy
                # after the current theme is re-staged from its templates.
                generated = generate(self.source)
                refreshed = command_result(["omarchy", "theme", "refresh"], project_root(self.source))
                self.send_json({**studio_state(self.source), "result": {"generated": generated, "refreshed": refreshed}})
                return

            self.send_error(HTTPStatus.NOT_FOUND)
        except StudioError as error:
            self.send_error_json(error)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tune canonical UI tokens in a local studio")
    parser.add_argument(
        "--source",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("WHITE_PILL_SOURCE", DEFAULT_SOURCE)).expanduser(),
        help="canonical TOML token file",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4777)
    return parser.parse_args()


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    args = parse_args()
    source = args.source.resolve()
    read_tokens(source)
    server = ThreadingHTTPServer((args.host, args.port), StudioHandler)
    server.source = source  # type: ignore[attr-defined]
    print(f"White Pill Studio → http://{args.host}:{args.port}")
    print(f"Token source      → {source}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
