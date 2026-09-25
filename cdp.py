"""A tiny headless-Chromium driver over the DevTools protocol (stdlib only).

    browser = cdp.launch("http://127.0.0.1:4777/#shell")
    try:
        browser.evaluate("document.title")
        browser.screenshot("page.png", clip={"x": 0, "y": 0, "width": 400, "height": 300})
    finally:
        browser.close()

`launch` starts Chromium with a throwaway profile at a fixed viewport and
device scale factor, enables Runtime/Page events before navigating (so load
exceptions are recorded), and waits for the load event. Beyond the stable
`evaluate` / `screenshot` / `close`, `call` sends raw protocol commands,
`wait` polls an expression, and `exceptions` lists page errors seen so far.
`Browser(websocket_url)` alone attaches to a page of a browser someone else runs.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request

__all__ = ["CDPError", "Browser", "launch", "find_chromium", "free_port"]


class CDPError(Exception):
    pass


def find_chromium() -> str | None:
    """`$CHROMIUM`, else the first Chromium-family binary on PATH."""
    if os.environ.get("CHROMIUM"):
        return os.environ["CHROMIUM"]
    for name in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


class _Socket:
    """Just enough of RFC 6455 for CDP: masked text frames out, whole frames in."""

    def __init__(self, websocket_url: str, timeout: float):
        host_port, path = websocket_url.removeprefix("ws://").split("/", 1)
        host, port = host_port.rsplit(":", 1)
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET /{path} HTTP/1.1\r\nHost: {host_port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        self.buffer = b""
        while b"\r\n\r\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("DevTools websocket handshake failed")
            self.buffer += chunk
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise CDPError(f"DevTools websocket refused: {head[:80]!r}")

    def send(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        mask = os.urandom(4)
        length = len(data)
        if length < 126:
            header = bytes([0x81, 0x80 | length])
        elif length < 65536:
            header = bytes([0x81, 0x80 | 126]) + struct.pack(">H", length)
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self.sock.sendall(header + mask + masked)

    def _read(self, count: int) -> bytes:
        while len(self.buffer) < count:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise CDPError("DevTools connection closed")
            self.buffer += chunk
        out, self.buffer = self.buffer[:count], self.buffer[count:]
        return out

    def receive(self) -> dict | None:
        first, second = self._read(2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read(8))[0]
        payload = self._read(length)
        if first & 0x0F != 0x1:  # ignore ping/pong/continuation; CDP sends whole text frames
            return None
        return json.loads(payload)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(5)


class Browser:
    """One page target; with `process` and `profile` (from `launch`), close() also stops that browser."""

    def __init__(self, websocket_url: str, timeout: float = 30, process: subprocess.Popen | None = None,
                 profile: tempfile.TemporaryDirectory | None = None):
        self.process = process
        self._profile = profile
        self._socket = _Socket(websocket_url, timeout)
        self._counter = 0
        self.events: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def call(self, method: str, **params):
        """Send one protocol command and return its result; events are queued in `events`."""
        self._counter += 1
        ident = self._counter
        self._socket.send({"id": ident, "method": method, "params": params})
        while True:
            message = self._socket.receive()
            if message is None:
                continue
            if message.get("id") == ident:
                if "error" in message:
                    raise CDPError(f"{method}: {message['error']}")
                return message.get("result", {})
            self.events.append(message)

    def evaluate(self, expression: str):
        """Evaluate JavaScript in the page (awaiting promises) and return its JSON value."""
        result = self.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise CDPError(f"Script error: {details.get('exception', {}).get('description') or details.get('text')}")
        return result["result"].get("value")

    def wait(self, expression: str, what: str, timeout: float = 4.0):
        """Poll `expression` until it is truthy; return its value."""
        deadline = time.monotonic() + timeout
        while True:
            value = self.evaluate(expression)
            if value:
                return value
            if time.monotonic() >= deadline:
                raise CDPError(f"Timed out waiting for {what}")
            time.sleep(0.05)

    def screenshot(self, path, clip: dict | None = None) -> pathlib.Path:
        """Write a PNG of the viewport, or of `clip` ({x, y, width, height} in CSS px, page coordinates)."""
        params = {"format": "png", "captureBeyondViewport": clip is not None}
        if clip is not None:
            params["clip"] = {**{key: clip[key] for key in ("x", "y", "width", "height")}, "scale": 1}
        data = self.call("Page.captureScreenshot", **params)["data"]
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))
        return path

    def exceptions(self) -> list[str]:
        """Uncaught exceptions and console errors/asserts seen since launch."""
        self.call("Runtime.evaluate", expression="0")  # flush pending events
        found = []
        for event in self.events:
            method, params = event.get("method"), event.get("params", {})
            if method == "Runtime.exceptionThrown":
                details = params.get("exceptionDetails", {})
                found.append(details.get("exception", {}).get("description") or details.get("text", "exception"))
            elif method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "assert"):
                found.append("console.error: " + " ".join(
                    str(arg.get("value", arg.get("description", ""))) for arg in params.get("args", [])))
        return found

    def close(self) -> None:
        self._socket.close()
        if self.process is not None:
            _stop(self.process)
        if self._profile is not None:
            self._profile.cleanup()


def launch(url: str, *, chromium: str | None = None, width: int = 1600, height: int = 1000,
           scale: float = 1, timeout: float = 15) -> Browser:
    """Start headless Chromium, open `url`, and wait for its load event."""
    binary = chromium or find_chromium()
    if not binary:
        raise CDPError("No Chromium binary found (set $CHROMIUM)")
    profile = tempfile.TemporaryDirectory(prefix="white-pill-cdp-", ignore_cleanup_errors=True)
    port = free_port()
    try:
        process = subprocess.Popen(
            [binary, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions", "--font-render-hinting=none",
             f"--force-device-scale-factor={scale}", f"--user-data-dir={profile.name}",
             f"--remote-debugging-port={port}", f"--window-size={width},{height}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        profile.cleanup()
        raise CDPError(f"Could not start {binary}: {error}") from error

    browser = None
    deadline = time.monotonic() + timeout
    try:
        while browser is None:
            if process.poll() is not None:
                raise CDPError(f"{binary} exited during startup ({process.returncode})")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1) as response:
                    targets = json.load(response)
                page = next(target for target in targets if target.get("type") == "page")
                browser = Browser(page["webSocketDebuggerUrl"], process=process, profile=profile)
            except (OSError, StopIteration, ValueError):
                if time.monotonic() > deadline:
                    raise CDPError("Chromium DevTools endpoint did not come up") from None
                time.sleep(0.1)
        browser.call("Runtime.enable")
        browser.call("Page.enable")
        browser.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                     deviceScaleFactor=scale, mobile=False)
        browser.call("Page.navigate", url=url)
        browser.wait("location.href !== 'about:blank' && document.readyState === 'complete'", f"{url} to load", timeout)
        return browser
    except BaseException:
        if browser is not None:
            browser.close()
        else:
            _stop(process)
            profile.cleanup()
        raise
