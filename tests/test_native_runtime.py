import importlib.util
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
    assert "NetworkNamespacePath=/run/netns/cb-browser" in api
    assert "CB_NETWORK_ISOLATED=true" in api and "CB_DEVELOPMENT=false" in api
    assert "User=cb-api" in api and "CB_BROWSER_GROUP=cb-browser" in api
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
