"""Green repro tests confirm a BLOCKED engine, not Obscura feature support."""

import hashlib
import importlib.util
import io
import json
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "obscura_gate", Path(__file__).parents[1] / "scripts/obscura_gate.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

BLOCKERS = {
    "isolated_world_not_page_global",
    "native_geometry_resists_page_override",
    "snapshot_geometry_matches_render",
    "native_hit_test_available",
    "native_input_resists_hit_target_override",
}


def test_release_pins_are_explicit_and_no_beta_drission_change():
    manifest = json.loads(gate.MANIFEST.read_text("utf-8"))
    assert manifest["tag"] == "v0.2.3"
    assert manifest["commit"] == "1a3169da276d7720732c7b20535474942917fb83"
    assert set(manifest["assets"]) == {"windows-amd64", "linux-amd64", "linux-arm64"}
    assert all(len(asset["sha256"]) == 64 for asset in manifest["assets"].values())
    assert 'DrissionPage==4.1.1.4' in (gate.ROOT / "pyproject.toml").read_text("utf-8")


def test_recorded_evidence_matches_probe_and_does_not_claim_admission():
    results = gate.ROOT / "experiments/obscura/results"
    obscura = json.loads((results / "windows-obscura-v023.json").read_text("utf-8"))
    chromium = json.loads((results / "windows-chromium-control.json").read_text("utf-8"))
    for result in (obscura, chromium):
        assert result["probe_sha256"] == hashlib.sha256(Path(gate.__file__).read_bytes()).hexdigest()
        assert result["fixture_sha256"] == hashlib.sha256(gate.FIXTURE.read_bytes()).hexdigest()
    assert not obscura["admitted"]
    assert {item["name"] for item in obscura["gates"] if not item["passed"]} == BLOCKERS
    assert chromium["admitted"]


def test_unverified_archive_is_rejected_before_parsing(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    archive.write_bytes(b"not an archive")
    with pytest.raises(ValueError, match="SHA256"):
        gate.unpack_release(archive, tmp_path, {"sha256": "0" * 64})


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_extraction_uses_fixed_names_never_archive_paths(tmp_path, kind):
    archive = tmp_path / f"test.{kind}"
    destination = tmp_path / "destination"
    destination.mkdir()
    suffix = ".exe" if kind == "zip" else ""
    names = ["../../obscura" + suffix, "sub/obscura-worker" + suffix, "../../unrelated"]
    if kind == "zip":
        with zipfile.ZipFile(archive, "w") as source:
            for name in names:
                source.writestr(name, b"synthetic")
    else:
        with tarfile.open(archive, "w:gz") as source:
            for name in names:
                info = tarfile.TarInfo(name)
                info.size = 9
                source.addfile(info, io.BytesIO(b"synthetic"))
    asset = {"name": "test.zip" if kind == "zip" else "test.tar.gz",
             "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    binary = gate.unpack_release(archive, destination, asset)
    assert binary.read_bytes() == b"synthetic"
    assert {path.name for path in destination.iterdir()} == {
        "obscura" + suffix, "obscura-worker" + suffix,
    }
    assert not (tmp_path.parent / "unrelated").exists()


@pytest.mark.parametrize("entries", [
    [("obscura", 1, "a")],
    [("obscura", 1, "a"), ("other/obscura", 1, "b"), ("obscura-worker", 1, "c")],
    [("obscura", 201 * 1024 * 1024, "a"), ("obscura-worker", 1, "b")],
])
def test_incomplete_duplicate_oversized_archives_fail_before_writes(tmp_path, entries):
    with pytest.raises(ValueError):
        gate._copy_release_members(lambda _: io.BytesIO(b"x"), entries,
                                   {"obscura", "obscura-worker"}, tmp_path)
    assert not list(tmp_path.iterdir())


def test_existing_file_cannot_be_overwritten(tmp_path):
    target = tmp_path / "obscura"
    target.write_bytes(b"user-owned")
    entries = [("obscura", 1, "a"), ("obscura-worker", 1, "b")]
    with pytest.raises(FileExistsError):
        gate._copy_release_members(lambda _: io.BytesIO(b"x"), entries,
                                   {"obscura", "obscura-worker"}, tmp_path)
    assert target.read_bytes() == b"user-owned"


def test_cdp_errors_are_not_empty_success():
    class Socket:
        def send(self, _):
            pass

        def recv(self, **_):
            return json.dumps({"id": 1, "error": {"code": -32601, "message": "unsupported"}})

    with pytest.raises(gate.CdpError, match="unsupported"):
        gate.Cdp(Socket()).call("DOM.getNodeForLocation", x=1, y=1)


def test_plain_binary_without_platform_digest_is_rejected(tmp_path, monkeypatch):
    binary = tmp_path / "obscura"
    binary.write_bytes(b"wrong binary")
    monkeypatch.setattr(gate, "platform_key", lambda: "linux-arm64")
    with pytest.raises(ValueError, match="unverified binary refused"):
        gate.run("obscura", binary)


@pytest.mark.skipif(not os.environ.get("OBSCURA_TEST_ARCHIVE"), reason="explicit pinned archive needed")
def test_obscura_v023_reproduces_blockers_not_an_admission_pass(tmp_path):
    manifest = json.loads(gate.MANIFEST.read_text("utf-8"))
    asset = manifest["assets"][gate.platform_key()]
    binary = gate.unpack_release(Path(os.environ["OBSCURA_TEST_ARCHIVE"]), tmp_path, asset)
    result = gate.run("obscura", binary, verified_archive=True)
    assert result["admitted"] is False
    assert {item["name"] for item in result["gates"] if not item["passed"]} == BLOCKERS


@pytest.mark.browser
@pytest.mark.skipif(not os.environ.get("CB_TEST_CHROMIUM"), reason="explicit test Chromium needed")
def test_chromium_passes_the_same_synthetic_gate():
    result = gate.run("chromium", Path(os.environ["CB_TEST_CHROMIUM"]))
    assert result["admitted"] is True, result["gates"]
