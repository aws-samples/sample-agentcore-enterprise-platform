"""docs/PLATFORM_YAML.md is generated from the pydantic models and must not rot.

The generator refuses to write a wrong page (its own consistency guards), and
this test refuses a stale one: the committed file must equal what the current
models produce, every field must carry a real description, and every preset
must be embedded so the reference and the presets cannot disagree.
"""

from __future__ import annotations

import importlib.util
import subprocess  # nosec B404 — running our own generator, no shell
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GEN = REPO / "scripts" / "gen_platform_reference.py"
DOC = REPO / "docs" / "PLATFORM_YAML.md"

_spec = importlib.util.spec_from_file_location("gen_platform_reference", GEN)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # nosec B603
        [sys.executable, str(GEN), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_committed_reference_is_current():
    result = run("--check")
    assert result.returncode == 0, (
        f"docs/PLATFORM_YAML.md is stale:\n{result.stdout}{result.stderr}"
    )


def test_every_field_is_described():
    doc = DOC.read_text()
    assert gen.TODO not in doc, "a field has no description in the generator's table"


def test_every_top_level_block_has_a_section():
    doc = DOC.read_text()
    from infra_utils.platform_config import PlatformConfig

    for name, field in PlatformConfig.model_fields.items():
        inner, _ = gen._unwrap_optional(field.annotation)
        if gen._is_model(inner):
            assert f"## `{name}:`" in doc, name


def test_every_preset_is_embedded():
    doc = DOC.read_text()
    for preset in sorted((REPO / "presets").glob("*.yaml")):
        assert preset.name in doc, preset.name
        # verbatim: the first non-comment line of the preset appears in the page
        body = [
            l for l in preset.read_text().splitlines() if l and not l.startswith("#")
        ]
        assert body[0] in doc, f"{preset.name} not embedded verbatim"


def test_a_hand_edit_fails_the_check(tmp_path, monkeypatch, capsys):
    # Point the generator at a mutated copy; --check must fail and show the diff.
    stale = tmp_path / "PLATFORM_YAML.md"
    stale.write_text(DOC.read_text().replace("reference", "refrence", 1))
    monkeypatch.setattr(gen, "OUT", stale)
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    assert gen.main(["--check"]) == 1
    out = capsys.readouterr().out
    assert "refrence" in out and "is stale" in out


def test_render_is_deterministic():
    assert gen.render() == gen.render()
