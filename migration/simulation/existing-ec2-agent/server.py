"""HTTP front for the simulated customer agent — the migration story's "before".

This is what the customer's agent looks like as a service today: its own HTTP
contract (POST /run, GET /healthz) on its own port. The migration adapter
(migration/adapter/) fronts this unchanged with the AgentCore contract.

    POST /run      {"prompt": "..."}  →  {"answer": "..."}
    GET  /healthz                     →  {"status": "ok"}

Port from PORT (default 8000). Honours MODEL_ID when set (so a migrated deploy
can lift the agent off its hard-coded Legacy model id without editing agent.py,
which keeps the "legacy code untouched" story intact).
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_agent = None


def get_agent():
    """Lazy so the server starts (and /healthz answers) without AWS access."""
    global _agent
    if _agent is None:
        model_id = os.environ.get("MODEL_ID", "")
        if model_id:
            from strands import Agent
            from strands.models.bedrock import BedrockModel

            _agent = Agent(
                model=BedrockModel(
                    model_id=model_id,
                    region_name=os.environ.get("AWS_REGION", "us-east-1"),
                ),
                system_prompt="You are a helpful assistant for ACME Corp. "
                "You help employees with research and code tasks.",
            )
        else:
            from agent import create_agent

            _agent = create_agent()
    return _agent


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/run":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            prompt = payload.get("prompt", "")
            response = get_agent()(prompt)
            self._send(200, {"answer": str(response)})
        except Exception as exc:  # noqa: BLE001 — request handler boundary: surface the real cause to the caller
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt, *args):
        print(f"[server] {fmt % args}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"ACME Corp Agent serving HTTP on :{port} (POST /run, GET /healthz)")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()  # nosec B104 — container service
