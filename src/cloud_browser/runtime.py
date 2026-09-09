"""Shared Docker/native process lifetime and data-directory ownership."""

import argparse
import contextlib
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path

from .models import BrowserError


class DataLock:
    """An OS lock shared by installations using the same data volume/inode."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.file = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / ".instance.lock"
        if target.is_symlink() or self.directory.is_symlink():
            raise RuntimeError("Data directory lock must not be a symlink")
        self.file = target.open("a+b")
        self.file.seek(0)
        if os.fstat(self.file.fileno()).st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise RuntimeError(
                "This data/profile volume is already owned by another instance"
            ) from exc
        return self

    def __exit__(self, *args):
        if self.file:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()
            self.file = None


class DisplayRuntime:
    def __init__(self, cfg):
        self.cfg = cfg
        self.processes = {}
        self.authority = None
        self.display = f":{cfg.display_number}"

    def activate_environment(self):
        # The worker creates Chromium serially. Select this work's X server
        # before launch; later control must not use another work's environment.
        if self.cfg.managed_display and not self.cfg.headless:
            os.environ["DISPLAY"] = self.display
            os.environ["XAUTHORITY"] = str(self.authority)

    @staticmethod
    def _spawn(argv, env=None):
        return subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def start(self):
        if not self.cfg.managed_display or self.cfg.headless:
            return
        if "display" in self.processes:
            if self.processes["display"].poll() is None:
                self.activate_environment()
                return
            raise BrowserError("DISPLAY_STOPPED", "Virtual display stopped; close the session")
        if os.name != "posix":
            raise BrowserError("UNSUPPORTED_OPERATION", "Managed display requires Linux")
        import grp

        display = self.display
        if Path(f"/tmp/.X11-unix/X{self.cfg.display_number}").exists():
            raise BrowserError(
                "DISPLAY_BUSY",
                "Configured display is already in use; do not attach to an unrelated display",
            )
        root = self.cfg.runtime_dir
        if root.is_symlink():
            raise BrowserError("POLICY_BLOCKED", "Runtime directory cannot be a symlink", "blocked")
        root.mkdir(parents=True, exist_ok=True, mode=0o750)
        gid = grp.getgrnam(self.cfg.browser_group).gr_gid
        os.chown(root, -1, gid)
        root.chmod(0o750)
        self.authority = root / ("xauth-" + secrets.token_hex(16))
        with self.authority.open("xb"):
            pass
        self.authority.chmod(0o600)
        try:
            subprocess.run(
                ["xauth", "-f", str(self.authority)],
                input=f"add {display} MIT-MAGIC-COOKIE-1 {secrets.token_hex(16)}\n",
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=5,
            )
            os.chown(self.authority, -1, gid)
            self.authority.chmod(0o640)
            os.environ["DISPLAY"] = display
            os.environ["XAUTHORITY"] = str(self.authority)
            self.processes["display"] = self._spawn(
                [
                    "Xvfb",
                    display,
                    "-screen",
                    "0",
                    f"{self.cfg.display_width}x{self.cfg.display_height}x24",
                    "-nolisten",
                    "tcp",
                    "-auth",
                    str(self.authority),
                    "-noreset",
                ]
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.processes["display"].poll() is not None:
                    break
                if Path(f"/tmp/.X11-unix/X{self.cfg.display_number}").exists():
                    return
                time.sleep(0.05)
            raise BrowserError("DISPLAY_UNAVAILABLE", "Virtual display did not start")
        except BaseException:
            self.close()
            raise

    def start_control(self):
        if not self.cfg.managed_display:
            return  # Legacy/operator-provided control bridge.
        if self.cfg.headless or "display" not in self.processes:
            raise BrowserError(
                "HANDOFF_UNAVAILABLE", "Manual control needs the same headed managed display"
            )
        if "vnc" in self.processes:
            return
        for port in (self.cfg.vnc_port, self.cfg.vnc_bridge_port):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    raise BrowserError(
                        "HANDOFF_UNAVAILABLE", "Private control port is already occupied"
                    )
        try:
            self.processes["vnc"] = self._spawn(
                [
                    "x11vnc",
                    "-display",
                    self.display,
                    "-auth",
                    str(self.authority),
                    "-localhost",
                    "-rfbport",
                    str(self.cfg.vnc_port),
                    "-nopw",
                    "-forever",
                    "-shared",
                    "-noxdamage",
                    "-quiet",
                    "-o",
                    "/dev/null",
                ]
            )
            self.processes["bridge"] = self._spawn(
                [
                    "websockify",
                    f"127.0.0.1:{self.cfg.vnc_bridge_port}",
                    f"127.0.0.1:{self.cfg.vnc_port}",
                ]
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if any(self.processes[key].poll() is not None for key in ("vnc", "bridge")):
                    break
                ready = []
                for port in (self.cfg.vnc_port, self.cfg.vnc_bridge_port):
                    with socket.socket() as probe:
                        ready.append(probe.connect_ex(("127.0.0.1", port)) == 0)
                if all(ready):
                    return
                time.sleep(0.05)
            raise BrowserError("HANDOFF_UNAVAILABLE", "Private control bridge did not start")
        except BaseException:
            self.stop_control()
            raise

    def _stop(self, key):
        process = self.processes.pop(key, None)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def stop_control(self):
        self._stop("bridge")
        self._stop("vnc")

    def close(self):
        self.stop_control()
        self._stop("display")
        if self.authority:
            self.authority.unlink(missing_ok=True)
            self.authority = None


def stop_browser_user(username):
    """Root helper: only the dedicated browser UID inside this exact service cgroup.

    No paths/PIDs from MCP, no host-wide user kill, no shell command construction.
    The installed sudo wrapper supplies the fixed dedicated username.
    """
    import pwd

    import psutil

    if os.geteuid() != 0:
        raise RuntimeError("Browser cleanup helper requires root")
    uid = pwd.getpwnam(username).pw_uid
    if uid == 0:
        raise RuntimeError("Browser cannot be root")
    group = Path("/proc/self/cgroup").read_text()
    victims = []
    for process in psutil.process_iter(["pid", "uids"]):
        try:
            if (
                process.info["uids"].real == uid
                and Path(f"/proc/{process.pid}/cgroup").read_text() == group
            ):
                victims.append(process)
                process.terminate()
        except (OSError, psutil.Error):
            continue
    _, alive = psutil.wait_procs(victims, timeout=2)
    for process in alive:
        with contextlib.suppress(psutil.Error):
            process.kill()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["stop-browser"])
    parser.add_argument("--user", required=True)
    args = parser.parse_args()
    stop_browser_user(args.user)
