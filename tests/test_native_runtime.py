import importlib.util
import os
from pathlib import Path

import pytest

from cloud_browser.runtime import DataLock


def installer():
    path = Path(__file__).parents[1] / "scripts/native_install.py"
    spec = importlib.util.spec_from_file_location("native_install_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_docker_and_native_cannot_open_same_data(tmp_path):
    with DataLock(tmp_path):
        with pytest.raises(RuntimeError, match="already owned"):
            with DataLock(tmp_path):
                pass
    with DataLock(tmp_path):
        assert (tmp_path / ".instance.lock").is_file()


def test_native_units_preserve_isolation_and_equal_budget():
    module = installer()
    units = module.render_units(
        {
            "network_cidr": "10.203.87.0/30",
            "namespace": "cb-browser",
            "data_dir": "/var/lib/cloud-browser-native",
            "memory_mib": 1024,
            "public_port": 18000,
            "control_port": 18001,
        },
        "/opt/cloud-browser/release/.venv/bin/python",
    )
    api = units["cloud-browser.service"].replace("\\", "/")
    assert "MemoryMax=1024M" in api and "MemorySwapMax=1024M" in api
    assert "TasksMax=512" in api
    assert "NetworkNamespacePath=/run/netns/cb-browser" in api
    assert "CB_NETWORK_ISOLATED=true" in api and "CB_DEVELOPMENT=false" in api
    assert "User=cb-api" in api and "CB_BROWSER_GROUP=cb-browser" in api
    assert "UMask=0007" in api  # Drission-created profile files must be browser-group readable.
    assert "CB_MANAGED_DISPLAY=true" in api and "KillMode=control-group" in api
    assert api.index("EnvironmentFile=/etc/cloud-browser/runtime.env") > api.index(
        "EnvironmentFile=/etc/cloud-browser/browser.env"
    )
    runtime = module.runtime_environment(units)
    assert "CB_PUBLIC_PORT=8000\n" in runtime and "CB_CHROMIUM_PATH=" in runtime
    assert "CB_ADMIN_PASSWORD_HASH" not in runtime and "CB_HEADLESS=false\n" in runtime
    assert "CB_INGRESS_BIND=127.0.0.1" in units["cloud-browser-ingress.service"]
    assert "CB_INGRESS_PUBLIC_PORT=18000" in units["cloud-browser-ingress.service"]
    assert all(
        "docker.service" not in text and "--no-sandbox" not in text for text in units.values()
    )


@pytest.mark.skipif(os.name != "posix", reason="Native config requires absolute POSIX paths")
def test_native_task_budget_is_bounded_and_preserves_operator_value():
    from types import SimpleNamespace

    module = installer()
    args = SimpleNamespace(
        cidr=None,
        memory_mib=None,
        public_port=None,
        control_port=None,
        data_dir=None,
        tasks_max=None,
    )
    assert module.native_config(args)["tasks_max"] == 512
    existing = module.native_config(args) | {"tasks_max": 384}
    assert module.native_config(args, existing)["tasks_max"] == 384
    args.tasks_max = 640
    cfg = module.native_config(args, existing)
    assert cfg["tasks_max"] == 640
    assert "TasksMax=640" in module.render_units(cfg, "/fixture/python")["cloud-browser.service"]
    for invalid in (0, -1, 127, 4097):
        args.tasks_max = invalid
        with pytest.raises(RuntimeError, match="Task budget"):
            module.native_config(args, existing)


def test_installer_has_no_docker_lifecycle_mutations():
    text = (Path(__file__).parents[1] / "scripts/native_install.py").read_text()
    assert 'command("docker"' not in text
    assert '"--purge-data"' in text
    assert "Existing unowned data directory" in text
    assert "Preserve local unit customization" in text


def test_container_does_not_start_display_or_vnc_at_boot():
    root = Path(__file__).parents[1]
    entry = (root / "deploy/entrypoint.sh").read_text()
    assert "Xvfb :" not in entry and "x11vnc -" not in entry and "websockify --" not in entry
    assert "CB_MANAGED_DISPLAY=true" in entry
    assert "XAUTHORITY" in (root / "deploy/browser.sudoers").read_text()
    runtime = (root / "src/cloud_browser/runtime.py").read_text()
    assert '"-auth"' in runtime and '"-ac"' not in runtime
    assert "unset XDG_CONFIG_HOME" in (root / "deploy/browser-engine").read_text()
    assert (
        "unset CHROME_CONFIG_HOME CHROME_USER_DATA_DIR"
        in (root / "deploy/browser-engine").read_text()
    )
    assert "cloud-browser-engine" in (root / "deploy/browser.sudoers").read_text()


def test_cleanup_failure_blocks_new_browser(monkeypatch, tmp_path):
    import subprocess

    from cloud_browser.config import Settings
    from cloud_browser.models import BrowserError
    from cloud_browser.worker import Worker

    cfg = Settings(development=True, data_dir=tmp_path).model_copy(
        update={"development": False, "browser_cleanup_command": "/fixed/operator/helper"}
    )
    worker = Worker(cfg)

    def denied(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "sudo")

    monkeypatch.setattr(subprocess, "run", denied)
    with pytest.raises(BrowserError) as error:
        worker._cleanup_children()
    assert error.value.code == "CLEANUP_FAILED"
    with pytest.raises(BrowserError) as error:
        worker.start()
    assert error.value.code == "CLEANUP_FAILED"


def test_redact_quoted_json_and_escaped_secret():
    from cloud_browser.security import redact

    for text in (
        '{"password":"hidden"}',
        '{"api_key": "hidden"}',
        "'otp': 'hidden'",
        '{"secret":"hidden\\"still-hidden"}',
    ):
        result = redact(text)
        assert "hidden" not in result and "[REDACTED]" in result


def test_memory_sampler_ignores_smaps_address_header(tmp_path):
    path = Path(__file__).parents[1] / "scripts/sample_memory.py"
    spec = importlib.util.spec_from_file_location("sample_memory_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "smaps_rollup"
    source.write_text("7f0-7f1 ---p 00000000 00:00 0 [rollup]\nRss: 42 kB\nPss: 31 kB\n")
    assert module.counters(source, kib=True) == {"Rss": 42 * 1024, "Pss": 31 * 1024}


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory and umask semantics")
def test_native_public_paths_repair_077_ancestors_without_exposing_secrets(tmp_path):
    module = installer()
    previous = os.umask(0o077)
    try:
        prefix = tmp_path / "opt" / "cloud-browser"
        prefix.mkdir(parents=True)
        private = prefix / "operator-private"
        private.mkdir()
        credential = private / "browser.env"
        module.write(credential, "fixture-not-a-real-secret\n", 0o640)
        module.public_directory(prefix)
        target = prefix / "releases" / "fixture" / "src" / "cloud_browser"
        module.public_directory(target)
        etc = tmp_path / "etc" / "cloud-browser"
        module.public_directory(etc)
        assert all(
            path.stat().st_mode & 0o777 == 0o755
            for path in (prefix, target, target.parent, target.parent.parent, etc)
        )
        assert private.stat().st_mode & 0o777 == 0o700
        assert credential.stat().st_mode & 0o777 == 0o640
        probe = tmp_path / "still-private"
        probe.mkdir()
        assert probe.stat().st_mode & 0o777 == 0o700
    finally:
        os.umask(previous)


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory and umask semantics")
@pytest.mark.parametrize("fail", [False, True])
def test_public_code_umask_restored_after_success_or_failure(tmp_path, fail):
    module = installer()
    previous = os.umask(0o077)
    try:
        try:
            with module.public_code_creation():
                public = tmp_path / "venv"
                public.mkdir()
                (public / "module.py").write_text("# fixture\n")
                if fail:
                    raise RuntimeError("fixture")
        except RuntimeError:
            assert fail
        assert public.stat().st_mode & 0o777 == 0o755
        assert (public / "module.py").stat().st_mode & 0o777 == 0o644
        private = tmp_path / "private"
        private.mkdir()
        assert private.stat().st_mode & 0o777 == 0o700
    finally:
        os.umask(previous)


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory and umask semantics")
def test_public_directory_rejects_symlink_and_writable_tree(tmp_path):
    module = installer()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(private, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlinks"):
        module.public_directory(alias / "child")
    assert private.stat().st_mode & 0o777 == 0o700
    writable = tmp_path / "writable"
    writable.mkdir()
    writable.chmod(0o777)
    with pytest.raises(RuntimeError, match="protected"):
        module.public_directory(writable)
