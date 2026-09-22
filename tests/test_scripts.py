"""The OpenAPI sync and check scripts, and the package metadata."""

import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

from meter_driver_emulator import SPEC_VERSION, __version__

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def test_installed_distribution_version_matches_package():
    assert importlib.metadata.version("meter-driver-emulator") == __version__


def run_script(path, *args, cwd=None):
    return subprocess.run(
        [sys.executable, str(path), *args], capture_output=True, text=True, timeout=30, cwd=cwd
    )


def layout(tmp_path, spec_version, openapi_text):
    """A minimal repository layout the scripts resolve relative to their own location."""
    package = tmp_path / "src" / "meter_driver_emulator"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f'SPEC_VERSION = "{spec_version}"\n', encoding="utf-8")
    if openapi_text is not None:
        (package / "openapi.json").write_text(openapi_text, encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(SCRIPTS / "check_openapi.py", scripts / "check_openapi.py")
    shutil.copy(SCRIPTS / "sync_openapi.py", scripts / "sync_openapi.py")
    return scripts


def test_check_openapi_passes_on_the_committed_document():
    completed = run_script(SCRIPTS / "check_openapi.py")
    assert completed.returncode == 0, completed.stderr
    assert (
        completed.stdout.strip()
        == f"openapi.json info.version {SPEC_VERSION} matches SPEC_VERSION {SPEC_VERSION}"
    )


def test_check_openapi_fails_on_version_mismatch(tmp_path):
    scripts = layout(tmp_path, "9.9.9", json.dumps({"openapi": "3.1.0", "info": {"version": "1.4.0"}}))
    completed = run_script(scripts / "check_openapi.py")
    assert completed.returncode == 1
    assert "does not match SPEC_VERSION 9.9.9" in completed.stderr


def test_check_openapi_diagnoses_missing_or_invalid_document(tmp_path):
    scripts = layout(tmp_path, "1.4.0", None)
    completed = run_script(scripts / "check_openapi.py")
    assert completed.returncode == 1
    assert "missing" in completed.stderr

    scripts = layout(tmp_path / "invalid", "1.4.0", "{not json")
    completed = run_script(scripts / "check_openapi.py")
    assert completed.returncode == 1
    assert "not valid JSON" in completed.stderr

    scripts = layout(tmp_path / "noversion", "1.4.0", json.dumps({"openapi": "3.1.0", "info": {}}))
    completed = run_script(scripts / "check_openapi.py")
    assert completed.returncode == 1
    assert "info.version is missing" in completed.stderr


def test_sync_openapi_converts_yaml_to_json(tmp_path):
    scripts = layout(tmp_path, "1.4.0", None)
    source = tmp_path / "spec.yaml"
    source.write_text(
        "openapi: 3.1.0\ninfo:\n  title: Tiny\n  version: '2.0.0'\npaths:\n  /v1/healthz:\n    get: {}\n",
        encoding="utf-8",
    )
    completed = run_script(scripts / "sync_openapi.py", str(source))
    assert completed.returncode == 0, completed.stderr
    assert "spec version 2.0.0" in completed.stdout
    output = tmp_path / "src" / "meter_driver_emulator" / "openapi.json"
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "openapi": "3.1.0",
        "info": {"title": "Tiny", "version": "2.0.0"},
        "paths": {"/v1/healthz": {"get": {}}},
    }
    assert output.read_text(encoding="utf-8").endswith("\n")


def test_sync_openapi_rejects_missing_file_and_non_openapi_input(tmp_path):
    scripts = layout(tmp_path, "1.4.0", None)
    completed = run_script(scripts / "sync_openapi.py", str(tmp_path / "absent.yaml"))
    assert completed.returncode == 1
    assert "cannot read" in completed.stderr

    not_openapi = tmp_path / "list.yaml"
    not_openapi.write_text("- just\n- a list\n", encoding="utf-8")
    completed = run_script(scripts / "sync_openapi.py", str(not_openapi))
    assert completed.returncode == 1
    assert "not an OpenAPI document" in completed.stderr

    bad_info = tmp_path / "badinfo.yaml"
    bad_info.write_text("openapi: 3.1.0\ninfo: just a string\n", encoding="utf-8")
    completed = run_script(scripts / "sync_openapi.py", str(bad_info))
    assert completed.returncode == 1
    assert "not an OpenAPI document" in completed.stderr
    assert not (tmp_path / "src" / "meter_driver_emulator" / "openapi.json").exists()
