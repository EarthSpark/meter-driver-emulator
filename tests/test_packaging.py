"""The build hook that generates openapi.json, and the package metadata.

hatch_build.py is not importable as an installed module, so it is loaded from
the repository root by path. Its generation tests build a miniature tree in a
temp directory and never touch the real submodule.
"""

import importlib.metadata
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from meter_driver_emulator import SPEC_VERSION, __version__

ROOT = Path(__file__).resolve().parent.parent

TINY_SPEC = "openapi: 3.1.0\ninfo:\n  title: Tiny\n  version: '2.0.0'\npaths:\n  /v1/healthz:\n    get: {}\n"
TINY_DOCUMENT = {
    "openapi": "3.1.0",
    "info": {"title": "Tiny", "version": "2.0.0"},
    "paths": {"/v1/healthz": {"get": {}}},
}
TINY_JSON = json.dumps(TINY_DOCUMENT, indent=2) + "\n"


def load_hatch_build():
    """Import hatch_build.py from the repository root under a private module name."""
    spec = importlib.util.spec_from_file_location("_hatch_build_under_test", ROOT / "hatch_build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


hatch_build = load_hatch_build()


def spec_tree(tmp_path, spec_yaml=None, existing_json=None):
    """A miniature source tree with an optional spec submodule and generated file."""
    package = tmp_path / "src" / "meter_driver_emulator"
    package.mkdir(parents=True)
    if existing_json is not None:
        (package / "openapi.json").write_text(existing_json, encoding="utf-8")
    if spec_yaml is not None:
        openapi_dir = tmp_path / "meter-driver-spec" / "openapi"
        openapi_dir.mkdir(parents=True)
        (openapi_dir / "meter-driver.yaml").write_text(spec_yaml, encoding="utf-8")
    return tmp_path


def test_installed_distribution_version_matches_package():
    assert importlib.metadata.version("meter-driver-emulator") == __version__


def test_spec_version_is_read_from_the_generated_document():
    """SPEC_VERSION is derived, so it cannot disagree with the document served."""
    generated = json.loads((ROOT / "src" / "meter_driver_emulator" / "openapi.json").read_text())
    assert SPEC_VERSION == generated["info"]["version"]


def test_the_generated_document_is_not_committed():
    """The spec is the source of truth; its conversion is a build artifact."""
    tracked = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/src/meter_driver_emulator/openapi.json" in tracked


def test_generate_converts_the_submodule_yaml(tmp_path):
    root = spec_tree(tmp_path, TINY_SPEC)
    assert hatch_build.generate(root) == "2.0.0"
    output = root / "src" / "meter_driver_emulator" / "openapi.json"
    assert output.read_text(encoding="utf-8") == TINY_JSON


def test_generate_overwrites_a_stale_document(tmp_path):
    stale = json.dumps({"openapi": "3.1.0", "info": {"title": "Old", "version": "1.0.0"}}, indent=2) + "\n"
    root = spec_tree(tmp_path, TINY_SPEC, existing_json=stale)
    assert hatch_build.generate(root) == "2.0.0"
    output = root / "src" / "meter_driver_emulator" / "openapi.json"
    assert output.read_text(encoding="utf-8") == TINY_JSON


def test_generate_keeps_an_existing_document_when_the_submodule_is_absent(tmp_path):
    """The sdist case: the generated file ships, the submodule does not."""
    root = spec_tree(tmp_path, None, existing_json=TINY_JSON)
    assert hatch_build.generate(root) == "2.0.0"
    output = root / "src" / "meter_driver_emulator" / "openapi.json"
    assert output.read_text(encoding="utf-8") == TINY_JSON


def test_generate_fails_when_neither_the_submodule_nor_a_document_exists(tmp_path):
    root = spec_tree(tmp_path, None)
    with pytest.raises(FileNotFoundError) as excinfo:
        hatch_build.generate(root)
    assert "git submodule update --init" in str(excinfo.value)


def test_convert_rejects_input_that_is_not_an_openapi_document():
    for bad in (["just", "a", "list"], {"info": {"version": "1"}}, {"openapi": "3.1.0", "info": "a string"}):
        with pytest.raises(ValueError, match="not an OpenAPI document"):
            hatch_build.convert(bad)


def test_convert_is_stable_json_with_a_trailing_newline():
    assert hatch_build.convert(TINY_DOCUMENT) == TINY_JSON
