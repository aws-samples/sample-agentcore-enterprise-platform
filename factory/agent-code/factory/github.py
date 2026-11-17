"""GitHub App access for the factory: App JWT -> down-scoped installation token -> REST calls.

The token is minted per run, lives only in this process, and is never logged or stored.
"""

import base64
import json
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

API = "https://api.github.com"
# What a CI fix needs. actions:read downloads job logs; workflows:write is required by GitHub
# for any push that touches .github/workflows/ (the Secret Boundary fix edits one).
PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "checks": "read",
    "actions": "read",
    "workflows": "write",
}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, private_key_pem: str, now: int | None = None) -> str:
    """RS256 JWT GitHub accepts for /app endpoints: valid 9 min, backdated 60 s for clock skew."""
    now = int(now if now is not None else time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(
        json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode()
    )
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    sig = key.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{claims}.{_b64(sig)}"


def request(
    method: str, path: str, token: str, body: dict | None = None, raw: bool = False
):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "software-factory",
        },
    )
    # Not forwarded on redirects: job-log downloads redirect to blob storage, which must never see the token.
    req.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(
            req, timeout=60
        ) as r:  # fixed https://api.github.com base
            data = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read(500).decode(errors="replace")
        raise RuntimeError(
            f"GitHub {method} {path.split('?')[0]} -> {e.code}: {detail}"
        ) from None
    return data if raw else json.loads(data or b"{}")


_LEVEL = {"read": 1, "write": 2, "admin": 3}


def installation_token(creds: dict, repo: str) -> tuple[str, list[str]]:
    """Token limited to one repository and the PERMISSIONS the installation grants; expires after 1 h.

    Asking for a permission the App was not granted fails the whole request (422), so the request is the
    intersection; the second value names what is missing so the run record can say why a step failed.
    """
    jwt = app_jwt(creds["app_id"], creds["private_key_pem"])
    installation = int(creds["installation_id"])
    granted = request("GET", f"/app/installations/{installation}", jwt).get(
        "permissions", {}
    )
    perms = {
        k: v
        for k, v in PERMISSIONS.items()
        if _LEVEL.get(granted.get(k, ""), 0) >= _LEVEL[v]
    }
    body = {"repositories": [repo.split("/", 1)[1]], "permissions": perms}
    resp = request(
        "POST", f"/app/installations/{installation}/access_tokens", jwt, body
    )
    return resp["token"], sorted(
        f"{k}:{v}" for k, v in PERMISSIONS.items() if k not in perms
    )


def verify_app(creds: dict, repo: str) -> dict:
    """Read-only check of the App: installation state, what it may reach, what its token can do.

    Returns names and permission levels only; the token stays in this function.
    """
    jwt = app_jwt(creds["app_id"], creds["private_key_pem"])
    inst = request("GET", f"/app/installations/{int(creds['installation_id'])}", jwt)
    token, missing = installation_token(creds, repo)
    repos = request("GET", "/installation/repositories?per_page=100", token)
    deliveries = request("GET", "/app/hook/deliveries?per_page=50", jwt)
    return {
        "installation_id": inst.get("id"),
        "account": (inst.get("account") or {}).get("login"),
        "repository_selection": inst.get("repository_selection"),
        "suspended": bool(inst.get("suspended_at")),
        "installation_permissions": inst.get("permissions", {}),
        "events": inst.get("events", []),
        "repositories": sorted(r["full_name"] for r in repos.get("repositories", [])),
        "missing_permissions": missing,
        "deliveries": [
            {
                k: d.get(k)
                for k in (
                    "event",
                    "action",
                    "status_code",
                    "delivered_at",
                    "redelivery",
                )
            }
            for d in deliveries
        ],
    }


def failed_job_logs(repo: str, run_id: int, token: str) -> str:
    jobs = request(
        "GET", f"/repos/{repo}/actions/runs/{int(run_id)}/jobs?filter=latest", token
    )["jobs"]
    logs = []
    for job in jobs:
        if job.get("conclusion") == "failure":
            text = request(
                "GET",
                f"/repos/{repo}/actions/jobs/{int(job['id'])}/logs",
                token,
                raw=True,
            )
            logs.append(text.decode(errors="replace")[-2_000_000:])
    return "\n".join(logs)


def open_pull_request(
    repo: str, token: str, head: str, base: str, title: str, body: str
) -> str:
    """Opens the PR, or returns the existing open one for the same branch (idempotent on retries)."""
    owner = repo.split("/", 1)[0]
    existing = request(
        "GET", f"/repos/{repo}/pulls?state=open&head={owner}:{head}", token
    )
    if existing:
        return existing[0]["html_url"]
    pr = request(
        "POST",
        f"/repos/{repo}/pulls",
        token,
        {"title": title, "body": body, "head": head, "base": base},
    )
    return pr["html_url"]
