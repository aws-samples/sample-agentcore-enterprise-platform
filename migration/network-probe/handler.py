"""Bounded TLS reachability probe for declared migration dependencies.

The invocation payload is intentionally ignored: callers cannot turn this
function into a VPC-side SSRF primitive. Hostnames come only from the
deployment manifest and results contain status codes, never resolved private
addresses, certificate contents, secret values, or exception text.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.exceptions import BotoCoreError, ClientError

HOSTS = tuple(json.loads(os.environ.get("MIGRATION_DEPENDENCY_HOSTS_JSON", "[]")))
CA_BUNDLE_SECRET_NAME = os.environ.get("MIGRATION_CA_BUNDLE_SECRET_NAME", "")
CONNECT_TIMEOUT_SECONDS = 5
MAX_WORKERS = 8


def tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not CA_BUNDLE_SECRET_NAME:
        return context

    response = boto3.client("secretsmanager").get_secret_value(
        SecretId=CA_BUNDLE_SECRET_NAME
    )
    bundle = response.get("SecretString")
    if bundle is None:
        binary = response.get("SecretBinary", b"")
        bundle = binary.decode("utf-8") if isinstance(binary, bytes) else ""
    if not bundle:
        raise ValueError("CA_BUNDLE_EMPTY")
    context.load_verify_locations(cadata=bundle)
    return context


def probe_host(host: str, context: ssl.SSLContext) -> dict[str, str]:
    """Resolve and complete a hostname-verified TLS handshake on port 443."""
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return {"host": host, "status": "DNS_FAILED"}

    try:
        with (
            socket.create_connection(
                (host, 443), timeout=CONNECT_TIMEOUT_SECONDS
            ) as connection,
            context.wrap_socket(connection, server_hostname=host),
        ):
            return {"host": host, "status": "PASS"}
    except TimeoutError:
        return {"host": host, "status": "CONNECT_TIMEOUT"}
    except ssl.SSLError:
        return {"host": host, "status": "TLS_FAILED"}
    except OSError:
        return {"host": host, "status": "CONNECT_FAILED"}


def handler(_event, _context) -> dict:
    """Probe only the immutable deployment allow-list, in bounded parallelism."""
    try:
        context = tls_context()
    except (ValueError, UnicodeDecodeError, BotoCoreError, ClientError):
        return {"ok": False, "configurationError": "CA_BUNDLE_UNAVAILABLE"}

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(HOSTS)))) as pool:
        results = list(pool.map(lambda host: probe_host(host, context), HOSTS))
    return {
        "ok": bool(results) and all(result["status"] == "PASS" for result in results),
        "results": results,
    }
