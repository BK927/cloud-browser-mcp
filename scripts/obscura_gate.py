"""Synthetic, raw-CDP admission probes. Not a production Obscura adapter.

Never attaches to an existing browser, reads a user profile, or loads an external
site. A dedicated child process and temporary storage are always cleaned up.
Exit 2 means the measured engine failed admission; exit 1 is a broken probe.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import quote
from urllib.request import ProxyHandler, build_opener

from PIL import Image
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "experiments/obscura/fixture.html"
MANIFEST = ROOT / "experiments/obscura/releases.json"


def platform_key():
    system = {"Windows": "windows", "Linux": "linux"}.get(platform.system())
    machine = {"AMD64": "amd64", "x86_64": "amd64", "aarch64": "arm64",
               "ARM64": "arm64"}.get(platform.machine())
    key = f"{system}-{machine}"
    if key not in json.loads(MANIFEST.read_text("utf-8"))["assets"]:
        raise ValueError("This experiment has no pinned asset for the current platform")
    return key


def unpack_release(archive, destination, asset):
    """Verify before parsing. Extract only two fixed regular files, never paths/links."""
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != asset["sha256"]:
        raise ValueError("Archive SHA256 does not match the pinned release")
    suffix = ".exe" if asset["name"].endswith(".zip") else ""
    required = {"obscura" + suffix, "obscura-worker" + suffix}
    if suffix:
        with zipfile.ZipFile(archive) as source:
            entries = [(item.filename, item.file_size, item)
                       for item in source.infolist() if not item.is_dir()]
            _copy_release_members(source.open, entries, required, destination)
    else:
        with tarfile.open(archive, "r:gz") as source:
            entries = [(item.name, item.size, item) for item in source.getmembers()
                       if item.isfile()]
            _copy_release_members(source.extractfile, entries, required, destination)
    return destination / ("obscura" + suffix)


def _copy_release_members(open_member, entries, required, destination):
    selected = {}
    for name, size, member in entries:
        basename = name.replace("\\", "/").rsplit("/", 1)[-1]
        if basename not in required:
            continue
        if basename in selected or not 0 < size <= 200 * 1024 * 1024:
            raise ValueError("Unexpected duplicate/oversized binary in release")
        selected[basename] = member
    if set(selected) != required:
        raise ValueError("Pinned release does not contain both required binaries")
    for basename, member in selected.items():
        target = destination / basename
        with open_member(member) as source, target.open("xb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        target.chmod(0o700)


class CdpError(RuntimeError):
    pass


class Cdp:
    def __init__(self, ws):
        self.ws = ws
        self.counter = 0
        self.session = None

    def call(self, method, **params):
        self.counter += 1
        packet = {"id": self.counter, "method": method, "params": params}
        if self.session:
            packet["sessionId"] = self.session
        self.ws.send(json.dumps(packet))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            reply = json.loads(self.ws.recv(timeout=max(0.01, deadline - time.monotonic())))
            if reply.get("id") != self.counter:
                continue
            if "error" in reply:
                raise CdpError(f"{method}: {reply['error']}")
            return reply.get("result", {})
        raise TimeoutError(method)

    def evaluate(self, expression, **params):
        reply = self.call("Runtime.evaluate", expression=expression, returnByValue=True, **params)
        if "exceptionDetails" in reply:
            raise CdpError("Synthetic script failed: " + str(reply["exceptionDetails"]))
        return reply["result"].get("value")

    def document(self):
        return self.call("DOM.getDocument", depth=-1)["root"]

    def node(self, name):
        # Native DOM tree only; never resolve a test result through page JavaScript.
        pending = [self.document()]
        while pending:
            node = pending.pop()
            attrs = node.get("attributes", [])
            if dict(zip(attrs[::2], attrs[1::2], strict=True)).get("id") == name:
                return node
            pending.extend(node.get("children", []))
        raise AssertionError(f"Fixture node missing: {name}")

    def click(self, x, y):
        self.call("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y,
                  button="left", clickCount=1)
        self.call("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y,
                  button="left", clickCount=1)

    def image(self):
        raw = base64.b64decode(self.call("Page.captureScreenshot", format="png")["data"])
        return Image.open(io.BytesIO(raw)).convert("RGB")


def attributes(node):
    values = node.get("attributes", [])
    return dict(zip(values[::2], values[1::2], strict=True))


def probe(cdp):
    target = cdp.call("Target.createTarget", url="about:blank")["targetId"]
    cdp.session = cdp.call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
    cdp.call("Page.enable")
    cdp.call("Emulation.setDeviceMetricsOverride", width=1024, height=768,
             deviceScaleFactor=1, mobile=False)
    cdp.call("Page.navigate", url="data:text/html;charset=utf-8," + quote(FIXTURE.read_text("utf-8")))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if cdp.node("safe"):
                break
        except AssertionError:
            time.sleep(0.05)
    cdp.node("otp")
    results = []

    def record(name, passed, **evidence):
        results.append({"name": name, "passed": bool(passed), "evidence": evidence})

    frame = cdp.call("Page.getFrameTree")["frameTree"]["frame"]["id"]
    context = cdp.call("Page.createIsolatedWorld", frameId=frame,
                       worldName="cloud-browser-gate")["executionContextId"]
    leaked = cdp.evaluate("typeof globalThis.__cb_page_marker", contextId=context)
    record("isolated_world_not_page_global", leaked == "undefined", marker_type=leaked)

    image_before = cdp.image()
    pixels = [list(image_before.getpixel(point)) for point in [(50, 50), (310, 50), (50, 190)]]
    record("rendered_fixture", image_before.size == (1024, 768) and pixels ==
           [[0, 180, 0], [220, 0, 0], [200, 0, 200]], size=list(image_before.size), pixels=pixels)
    if not results[-1]["passed"]:
        raise AssertionError("Fixture is not rendered as expected; subsequent gates invalid")

    otp = cdp.node("otp")["backendNodeId"]
    baseline = cdp.call("DOM.getBoxModel", backendNodeId=otp)["model"]["border"]
    ax = cdp.call("Accessibility.getFullAXTree")
    record("native_dom_and_full_ax", bool(ax.get("nodes")) and
           attributes(cdp.node("otp")).get("autocomplete") == "one-time-code",
           ax_node_count=len(ax.get("nodes", [])), otp_border=baseline)

    # This is ordinary page-world JavaScript, not a privileged engine mutation.
    cdp.evaluate("""(() => {
      document.getElementById('otp').getBoundingClientRect = () =>
        ({left:700, top:600, right:710, bottom:610, x:700, y:600, width:10, height:10});
    })()""")
    forged = cdp.call("DOM.getBoxModel", backendNodeId=otp)["model"]["border"]
    quads = cdp.call("DOM.getContentQuads", backendNodeId=otp)["quads"]
    image_after = cdp.image()
    record("native_geometry_resists_page_override", baseline == forged,
           before=baseline, after=forged, content_quads=quads,
           rendered_pixels_unchanged=image_before.tobytes() == image_after.tobytes())

    snapshot = cdp.call("DOMSnapshot.captureSnapshot", computedStyles=[])
    doc = snapshot["documents"][0]
    index = doc["nodes"]["backendNodeId"].index(cdp.node("otp")["backendNodeId"])
    layout_index = doc["layout"]["nodeIndex"].index(index)
    bounds = doc["layout"]["bounds"][layout_index]
    record("snapshot_geometry_matches_render", bounds == [40, 180, 200, 40], otp_bounds=bounds)
    try:
        hit = cdp.call("DOM.getNodeForLocation", x=50, y=50)
        hit_ok = hit.get("backendNodeId") == cdp.node("safe")["backendNodeId"]
    except CdpError as exc:
        hit, hit_ok = {"error": str(exc)}, False
    record("native_hit_test_available", hit_ok, result=hit)

    cdp.click(50, 50)
    record("baseline_click", attributes(cdp.node("safe")).get("aria-expanded") == "false" and
           "data-clicked" not in attributes(cdp.node("decoy")))
    if not results[-1]["passed"]:
        raise AssertionError("Baseline click failed; target-substitution probe would be invalid")
    cdp.evaluate("""(() => {
      document.getElementById('safe').removeAttribute('data-clicked');
      document.elementFromPoint = () => document.getElementById('decoy');
    })()""")
    cdp.click(50, 50)
    safe, decoy = attributes(cdp.node("safe")), attributes(cdp.node("decoy"))
    record("native_input_resists_hit_target_override", safe.get("data-clicked") == "yes" and
           "data-clicked" not in decoy, safe_clicked=safe.get("data-clicked"),
           decoy_clicked=decoy.get("data-clicked"))
    cdp.session = None
    cdp.call("Target.closeTarget", targetId=target)
    return results


def run(engine, binary, *, verified_archive=False):
    # No arbitrary remote attach, profile reuse, proxy, file:// or private-network enable.
    binary = Path(binary).resolve(strict=True)
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    with binary.open("rb") as source:
        binary_hash = hashlib.file_digest(source, "sha256").hexdigest()
    if engine == "obscura" and not verified_archive:
        expected = manifest["assets"][platform_key()].get("binary_sha256")
        if not expected or binary_hash != expected:
            raise ValueError("Use --archive with the pinned release (unverified binary refused)")
    with tempfile.TemporaryDirectory(prefix="cb-obscura-gate-") as folder:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "LANG", "DISPLAY"}}
        env.update({key: folder for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP")})
        if engine == "chromium" and os.name == "nt":
            # Windows shell known-folder APIs need real OS locations. The explicit
            # fresh --user-data-dir below, not these paths, owns browser state.
            for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
                if key in os.environ:
                    env[key] = os.environ[key]
        headers = {}
        if engine == "obscura":
            token = secrets.token_urlsafe(32)
            env["OBSCURA_CDP_TOKEN"] = token
            headers["Authorization"] = "Bearer " + token
            args = [str(binary), "serve", "--host", "127.0.0.1", "--port", str(port),
                    "--max-connections", "1", "--storage-dir", folder, "--quiet"]
        else:
            args = [str(binary), "--headless=new", f"--remote-debugging-port={port}",
                    "--remote-debugging-address=127.0.0.1", f"--user-data-dir={folder}/profile",
                    "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
                    "--disable-component-update", "about:blank"]
        engine_log = tempfile.TemporaryFile()
        proc = subprocess.Popen(args, cwd=folder, env=env, stdin=subprocess.DEVNULL,
                                stdout=engine_log, stderr=engine_log,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        ws = None
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    engine_log.seek(0)
                    detail = engine_log.read(8192).decode("utf-8", errors="replace")
                    if engine == "obscura":
                        detail = detail.replace(token, "[redacted]")
                    raise RuntimeError("Dedicated engine child exited before probe: " + detail)
                try:
                    if engine == "obscura":
                        url = f"ws://127.0.0.1:{port}/devtools/browser"
                    else:
                        with build_opener(ProxyHandler({})).open(
                                f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
                            url = json.load(response)["webSocketDebuggerUrl"]
                    ws = connect(url, additional_headers=headers, proxy=None, open_timeout=1,
                                 max_size=16 * 1024 * 1024)
                    break
                except (OSError, TimeoutError):
                    time.sleep(0.1)
            if ws is None:
                engine_log.seek(0)
                detail = engine_log.read(8192).decode("utf-8", errors="replace")
                if engine == "obscura":
                    detail = detail.replace(token, "[redacted]")
                raise TimeoutError("Dedicated engine did not start: " + detail)
            cdp = Cdp(ws)
            version = cdp.call("Browser.getVersion")
            gates = probe(cdp)
            return {"engine": engine, "platform": platform.system(),
                    "architecture": platform.machine(), "binary_sha256": binary_hash,
                    "obscura_release": manifest["tag"] if engine == "obscura" else None,
                    "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "browser_version": version, "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
                    "admitted": all(gate["passed"] for gate in gates), "gates": gates,
                    "scope": "synthetic data URL only; not production isolation or Pi performance"}
        finally:
            if ws:
                ws.close()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            engine_log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["obscura", "chromium"], required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--binary", type=Path)
    source.add_argument("--archive", type=Path, help="Pinned Obscura release archive for this platform")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.archive:
        if args.engine != "obscura":
            parser.error("--archive is only supported for Obscura")
        asset = json.loads(MANIFEST.read_text("utf-8"))["assets"][platform_key()]
        with tempfile.TemporaryDirectory(prefix="cb-obscura-release-") as folder:
            binary = unpack_release(args.archive, Path(folder), asset)
            report = run(args.engine, binary, verified_archive=True)
            report["archive_sha256"] = asset["sha256"]
    else:
        report = run(args.engine, args.binary)
    serialized = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        # Exclusive creation: never replace another run's evidence.
        with args.output.open("x", encoding="utf-8") as output:
            output.write(serialized + "\n")
    print(serialized)
    return 0 if report["admitted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
