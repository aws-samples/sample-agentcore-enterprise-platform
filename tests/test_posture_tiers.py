"""docs/POSTURE_TIERS.md must match what scripts/gen_posture_tiers.py generates."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "gen_posture_tiers", REPO_ROOT / "scripts" / "gen_posture_tiers.py"
)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def test_committed_doc_matches_generator():
    committed = gen.OUTPUT.read_text(encoding="utf-8")
    assert committed == gen.render(), (
        f"docs/POSTURE_TIERS.md is stale; run `{gen.REGENERATE_COMMAND}` and commit the result"
    )


def test_tier_mapping_points_at_existing_presets():
    assert [tier for tier, _ in gen.TIER_PRESETS] == [
        "workshop",
        "secure",
        "production",
    ]
    for _, path in gen.TIER_PRESETS:
        assert (REPO_ROOT / path).is_file(), path


def test_cedar_mode_differs_between_secure_and_production_and_is_kept():
    presets = dict(gen.TIER_PRESETS)
    secure = gen.load_preset(presets["secure"])
    production = gen.load_preset(presets["production"])
    cedar_keys = sorted(
        key
        for key in set(secure) | set(production)
        if "cedar" in key.lower() and "mode" in key.lower()
    )
    differing = [k for k in cedar_keys if secure.get(k) != production.get(k)]
    assert differing, (
        f"expected a Cedar mode key differing across tiers, got {cedar_keys}"
    )
    tiers = gen.load_tiers()
    rendered = gen.render(tiers)
    for key in differing:
        assert key in tiers["secure"] or key in tiers["production"]
        assert f"**`{key}`**" in rendered


def test_filter_excludes_selection_keys():
    assert gen.is_relevant("security.cedar.mode")
    assert gen.is_relevant("observability.alarms")
    assert not gen.is_relevant("accounts.platform")
    assert not gen.is_relevant("region")


def test_render_marks_absent_and_differing_rows():
    tiers = {
        "workshop": {"security.x": True, "observability.y": ["a", "b"]},
        "secure": {"observability.y": ["a", "b"]},
        "production": {"security.x": False, "observability.y": ["a", "b"]},
    }
    out = gen.render(tiers)
    assert "**`security.x`**" in out
    assert gen.ABSENT in out
    assert "| `observability.y` | `[a, b]` | `[a, b]` | `[a, b]` |" in out
    assert out.index("observability.y") < out.index("security.x")
