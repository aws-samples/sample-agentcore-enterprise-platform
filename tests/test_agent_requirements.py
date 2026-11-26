"""Guards against vulnerable or inconsistent agent dependency pins."""

import re
from pathlib import Path

from packaging.specifiers import SpecifierSet

AGENT_CODE = Path(__file__).resolve().parents[1] / "agent-code"

VULNERABLE = {
    "pyjwt": ["2.13.0", "2.14.0"],
    "mcp": ["1.29.0"],
}

# Lowest fixed release of each package. Pins are exact (==) so an unbounded range
# cannot pull in a new major version (mcp 2.x) at image build time.
MINIMUM = {"pyjwt": "2.15.1", "mcp": "1.30.0"}

_REQ_RE = re.compile(r"^([A-Za-z0-9_.-]+)(\[[^\]]*\])?\s*(.*)$")


def _requirement_files():
    return sorted(AGENT_CODE.glob("*/requirements.txt"))


def _parse(path):
    reqs = {}
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = _REQ_RE.match(line)
        if not match:
            continue
        name = match.group(1).lower().replace("_", "-")
        reqs[name] = (match.group(2) or "", match.group(3).replace(" ", ""))
    return reqs


def test_requirement_files_found():
    assert _requirement_files()


def test_pins_consistent_across_agents():
    for package in VULNERABLE:
        specs = {}
        for path in _requirement_files():
            reqs = _parse(path)
            if package in reqs:
                specs[path.parent.name] = reqs[package]
        assert specs, f"{package} not found in any requirements file"
        assert len(set(specs.values())) == 1, f"inconsistent {package} pins: {specs}"


def test_pins_exclude_vulnerable_versions():
    for path in _requirement_files():
        reqs = _parse(path)
        for package, bad_versions in VULNERABLE.items():
            if package not in reqs:
                continue
            spec = SpecifierSet(reqs[package][1])
            for bad in bad_versions:
                assert bad not in spec, f"{path} allows vulnerable {package}=={bad}"


def test_pins_are_exact_and_at_least_minimum():
    from packaging.version import Version

    for path in _requirement_files():
        reqs = _parse(path)
        for package, minimum in MINIMUM.items():
            if package not in reqs:
                continue
            spec = reqs[package][1]
            assert spec.startswith("=="), (
                f"{path} must pin {package} exactly, got {spec!r}"
            )
            assert Version(spec[2:]) >= Version(minimum), (
                f"{path} pins {package} below {minimum}"
            )


def test_pyjwt_keeps_crypto_extra():
    for path in _requirement_files():
        reqs = _parse(path)
        if "pyjwt" in reqs:
            assert reqs["pyjwt"][0] == "[crypto]", f"{path} drops PyJWT[crypto]"


def test_claude_sdk_dockerfiles_use_requirements_for_otel():
    for agent in ("claude-sdk-agent", "claude-sdk-multi-agent"):
        dockerfile = (AGENT_CODE / agent / "Dockerfile").read_text()
        assert "aws-opentelemetry-distro==" not in dockerfile
        reqs = _parse(AGENT_CODE / agent / "requirements.txt")
        assert "aws-opentelemetry-distro" in reqs
