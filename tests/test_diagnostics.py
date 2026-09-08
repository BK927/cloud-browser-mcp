import json

from cloud_browser.diagnostics import doctor_report, find_chromium, self_test


def test_doctor_reports_missing_configuration_without_echoing_secrets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CB_PUBLIC_ORIGIN", "https://user:never-print-me@example.com")
    monkeypatch.setenv("CB_ADMIN_PASSWORD_HASH", "also-never-print-this")
    report = doctor_report()
    encoded = json.dumps(report)
    assert not report["checks"]["configuration_valid"]
    assert not report["deployment_verified"] and not report["chatgpt_connection_verified"]
    assert "never-print" not in encoded


def test_malformed_env_json_is_reported_without_traceback_or_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CB_OAUTH_REDIRECT_URIS", "not-json-never-print-me")
    report = doctor_report()
    assert not report["checks"]["configuration_valid"]
    assert report["configuration_issues"][0]["type"] == "settings_parse_error"
    assert "never-print-me" not in json.dumps(report)


def test_explicit_executable_is_preferred(tmp_path):
    executable = tmp_path / "test-chromium"
    executable.touch()
    assert find_chromium(str(executable)) == str(executable)


async def test_invalid_explicit_self_test_path_does_not_launch_another_browser(tmp_path):
    result = await self_test(str(tmp_path / "missing-chromium"))
    assert not result["ok"] and result["error"] == "CHROMIUM_NOT_FOUND"
