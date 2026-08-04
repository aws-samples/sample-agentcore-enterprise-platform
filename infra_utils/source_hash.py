"""Source hashing for container rebuild triggers.

Deliberately free of CDK imports so it stays unit-testable without
aws-cdk-lib (see tests/test_runtime_image_tag.py).
"""

import hashlib
import os

# Directories/extensions excluded from source hash computation
HASH_EXCLUDES = {
    "node_modules",
    "__pycache__",
    ".git",
    ".venv",
    ".next",
    ".terraform",
    ".DS_Store",
    ".pyc",
    ".log",
    ".egg-info",
    "dist",
    "build",
    "cdk.out",
}


def compute_source_hash(source_path: str) -> str:
    """Stable SHA-256 over all source files, excluding non-source artifacts."""
    if not os.path.isdir(source_path):
        return "placeholder"
    file_hashes = []
    for root, dirs, files in os.walk(source_path):
        # Prune excluded directories in-place
        dirs[:] = [d for d in sorted(dirs) if d not in HASH_EXCLUDES]
        for fname in sorted(files):
            if any(fname.endswith(ext) for ext in (".pyc", ".log", ".DS_Store")):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, source_path)
            h = hashlib.sha256()
            h.update(rel.encode())
            with open(fpath, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            file_hashes.append(h.hexdigest())
    if not file_hashes:
        return "empty"
    combined = hashlib.sha256("".join(file_hashes).encode())
    return combined.hexdigest()[:16]


def component_image_tag(source_path: str, dockerfile_pattern: str = "") -> str:
    """Image tag / rebuild trigger: source content hash + the selected pattern.

    dockerfile_pattern selects WHICH Dockerfile is built out of the shared
    agent-code/ tree, so it has to be part of the tag. Hashing only file
    contents makes every pattern collide on one tag: switching agent_pattern
    leaves the trigger property unchanged (no CodeBuild run) and the runtime
    keeps serving the previously built pattern's image.
    """
    digest = compute_source_hash(source_path)
    if dockerfile_pattern:
        digest = hashlib.sha256(f"{digest}:{dockerfile_pattern}".encode()).hexdigest()[
            :16
        ]
    return digest
