"""In-VPC probe for the Claude apps gateway, invoked by verify.py.

The gateway is reachable only on the private network, so verify.py cannot
call it from an operator laptop that may be off the VPN. This function runs in
the platform VPC's private subnets and makes the calls Anthropic documents as
the health surface (https://code.claude.com/docs/en/claude-apps-gateway-deploy#health):

  GET /healthz                                  liveness
  GET /readyz                                   Postgres reachable
  GET /.well-known/oauth-authorization-server   200 only after config load, OIDC
                                                discovery, upstream construction
                                                and Postgres migration succeed

DNS-independent: it opens TCP to the ALB's own DNS name (CONNECT_HOST) and
speaks TLS with the gateway hostname as SNI and Host header, so the
certificate is still verified against the hostname developers use, whether
that name lives in Route 53 or only in the customer's corporate DNS.

/healthz and /readyz are exempt from the gateway's access_control.allow_cidrs;
the metadata path is not. With the in-gateway IP allow-list on
(EXPECT_METADATA_STATUS=403) this probe, not being a developer address, is
refused, and that refusal is what it checks: the allow-list is enforced.
"""

import http.client
import json
import os
import socket
import ssl
from urllib.parse import urlsplit

PUBLIC_URL = os.environ["PUBLIC_URL"]
HOSTNAME = urlsplit(PUBLIC_URL).hostname
CONNECT_HOST = os.environ.get("CONNECT_HOST") or HOSTNAME
EXPECT_METADATA_STATUS = int(os.environ.get("EXPECT_METADATA_STATUS", "200"))
# Only when the ALB certificate chains to a private CA this runtime does not
# trust: TLS is then not verified, the HTTP checks still are.
INSECURE_TLS = os.environ.get("INSECURE_TLS", "false") == "true"
METADATA = "/.well-known/oauth-authorization-server"
TIMEOUT = 4  # x 3 calls: inside the function timeout and verify's 20 s budget


class _PinnedConnection(http.client.HTTPSConnection):
    """HTTPS to `connect_host`, with SNI, certificate check and Host = self.host."""

    def __init__(self, connect_host: str, host: str, context: ssl.SSLContext):
        super().__init__(host, 443, timeout=TIMEOUT, context=context)
        self._connect_host = connect_host

    def connect(self):
        sock = socket.create_connection((self._connect_host, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if INSECURE_TLS:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # nosec B501 - explicit opt-in for private-CA certificates
    return ctx


def _get(path: str) -> tuple[int, str]:
    conn = _PinnedConnection(CONNECT_HOST, HOSTNAME, _context())
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read(4096).decode(errors="replace")
    finally:
        conn.close()


def handler(event, context):
    checks = {}
    for name, path in (
        ("healthz", "/healthz"),
        ("readyz", "/readyz"),
        ("oauth_metadata", METADATA),
    ):
        try:
            status, body = _get(path)
            checks[name] = {"status": status}
            if name == "oauth_metadata" and status == 200:
                issuer = json.loads(body).get("issuer", "")
                checks[name]["issuer"] = issuer
                checks[name]["issuer_matches_public_url"] = (
                    issuer.rstrip("/") == PUBLIC_URL
                )
        except Exception as e:  # noqa: BLE001 - DNS, TLS, timeout: report, never raise
            checks[name] = {"error": f"{type(e).__name__}: {e}"}

    meta = checks["oauth_metadata"]
    if EXPECT_METADATA_STATUS == 200:
        meta_ok = meta.get("status") == 200 and meta.get(
            "issuer_matches_public_url", False
        )
    else:
        meta_ok = meta.get("status") == EXPECT_METADATA_STATUS
    ok = (
        checks["healthz"].get("status") == 200
        and checks["readyz"].get("status") == 200
        and meta_ok
    )
    return {
        "ok": ok,
        "public_url": PUBLIC_URL,
        "connected_via": CONNECT_HOST,
        "allowlist_mode": EXPECT_METADATA_STATUS != 200,
        "checks": checks,
    }
