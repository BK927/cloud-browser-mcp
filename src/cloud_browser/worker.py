"""Bounded, single-flight IPC. Never retry a browser command after a timeout."""

import asyncio
import multiprocessing
import secrets
import signal
import subprocess

import psutil

from .config import Settings
from .models import BrowserError


def worker_main(connection, configuration):
    from .drission import DrissionAdapter

    adapter = None

    def stop(signum, frame):
        raise SystemExit

    signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            command = connection.recv()
            if command is None:
                return
            request_id, method, arguments = command
            try:
                if adapter is None:
                    try:
                        adapter = DrissionAdapter(Settings(**configuration))
                    except ImportError as exc:
                        raise BrowserError(
                            "ENGINE_UNAVAILABLE",
                            "Install the browser extra with the separately licensed DrissionPage engine",
                        ) from exc
                result = getattr(adapter, method)(**arguments)
                connection.send({"id": request_id, "result": result})
            except BrowserError as exc:
                connection.send(
                    {
                        "id": request_id,
                        "error": {
                            "code": exc.code,
                            "message": exc.message,
                            "status": exc.status,
                            "details": exc.details,
                        },
                    }
                )
            except Exception:
                # Exception messages/stack locals can contain URLs or typed secrets.
                connection.send(
                    {
                        "id": request_id,
                        "error": {
                            "code": "RESULT_UNCERTAIN" if method == "act" else "BROWSER_ERROR",
                            "message": "Browser command failed; inspect status before continuing",
                            "status": "error",
                            "details": {},
                        },
                    }
                )
    except (EOFError, BrokenPipeError):
        pass
    finally:
        if adapter:
            adapter.shutdown()
        connection.close()


class Worker:
    def __init__(self, settings: Settings):
        self.cfg = settings
        self.process = None
        self.connection = None
        self.lock = asyncio.Lock()
        self.children = []
        self.cleanup_failed = False

    def start(self):
        if self.cleanup_failed:
            raise BrowserError(
                "CLEANUP_FAILED",
                "Browser cleanup could not be verified; operator must restart this dedicated service",
            )
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self.process = context.Process(
            target=worker_main, args=(child, self.cfg.model_dump(mode="json")), daemon=True
        )
        self.process.start()
        child.close()
        self.connection = parent

    async def call(self, method, **arguments):
        async with self.lock:
            if self.process is None:
                self.start()
            if not self.process.is_alive():
                raise BrowserError("SESSION_EXPIRED", "Browser worker has stopped")
            try:
                request_id = secrets.token_hex(16)
                self.connection.send((request_id, method, arguments))
                ready = await asyncio.to_thread(self.connection.poll, 45)
                if not ready:
                    self._remember_children()
                    self.process.terminate()
                    raise BrowserError(
                        "RESULT_UNCERTAIN" if method == "act" else "WORKER_TIMEOUT",
                        "Worker timed out; no automatic retry was attempted",
                    )
                message = self.connection.recv()
                self._remember_children()
                if message.get("id") != request_id:
                    self.process.terminate()
                    raise BrowserError(
                        "SESSION_EXPIRED", "Worker response correlation failed; session invalidated"
                    )
            except asyncio.CancelledError:
                self._remember_children()
                if self.process and self.process.is_alive():
                    self.process.terminate()
                await asyncio.shield(self.shutdown())
                raise
            except (EOFError, BrokenPipeError, OSError) as exc:
                raise BrowserError(
                    "RESULT_UNCERTAIN" if method == "act" else "SESSION_EXPIRED",
                    "Worker disconnected; no automatic retry was attempted",
                ) from exc
            if "error" in message:
                e = message["error"]
                raise BrowserError(e["code"], e["message"], e["status"], **e["details"])
            return message["result"]

    def _remember_children(self):
        try:
            self.children = psutil.Process(self.process.pid).children(recursive=True)
        except psutil.Error:
            pass

    def _cleanup_children(self):
        # psutil checks PID creation time before signaling, avoiding PID reuse.
        for process in reversed(self.children):
            try:
                process.terminate()
            except psutil.Error:
                pass
        self.children.clear()
        if self.cfg.browser_cleanup_command and not self.cfg.development:
            # Fixed operator-installed helper kills only the dedicated browser UID
            # in this service cgroup, including orphans after a worker crash.
            try:
                subprocess.run(
                    ["sudo", "-n", self.cfg.browser_cleanup_command],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=True,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                self.cleanup_failed = True
                raise BrowserError(
                    "CLEANUP_FAILED",
                    "Dedicated browser cleanup failed; operator restart is required",
                ) from exc

    async def shutdown(self):
        if self.process:
            if self.process.is_alive():
                try:
                    self.connection.send(None)
                except (OSError, BrokenPipeError):
                    pass
                await asyncio.to_thread(self.process.join, 5)
                if self.process.is_alive():
                    self.process.terminate()
                    await asyncio.to_thread(self.process.join, 3)
            self.connection.close()
            self.process = None
            await asyncio.to_thread(self._cleanup_children)
