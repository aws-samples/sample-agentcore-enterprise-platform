# Adapted from fullstack-solution-template-for-agentcore
"""Core Code Interpreter tools for AgentCore."""

import json
import logging
import uuid

logger = logging.getLogger(__name__)


class CodeInterpreterTools:
    """Tools for code execution via AgentCore Code Interpreter.

    One session is opened lazily on the first execute_python_securely call and
    reused for the rest of the request, so variables and files carry across the
    model's tool calls (clearContext is False). The caller owns its lifetime:
    stop it with cleanup() in the entrypoint's ``finally`` block, or use the
    instance as a context manager around the agent run. An unstopped session
    keeps costing money and counts against the account quota until AgentCore
    times it out.
    """

    def __init__(self, region: str):
        """
        Initialize the code interpreter tools.

        Args:
            region: AWS region for code interpreter
        """
        self.region = region
        self._code_client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.cleanup()
        return False

    def _get_code_interpreter_client(self):
        """Get or create code interpreter client."""
        if self._code_client is None:
            from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

            self._code_client = CodeInterpreter(self.region)
            self._code_client.start()
            logger.info("Started code interpreter in %s", self.region)
        return self._code_client

    def cleanup(self):
        """Stop the code interpreter session, if one is open.

        Idempotent and never raises: a failing stop() is logged so it cannot
        mask the result (or the error) of the call that used the session.
        """
        client, self._code_client = self._code_client, None
        if client is None:
            return
        try:
            client.stop()
        except Exception:  # cleanup must not raise
            logger.warning("Failed to stop code interpreter session", exc_info=True)

    def execute_python_securely(self, code: str) -> str:
        """
        Execute Python code in a secure AgentCore CodeInterpreter sandbox.

        Args:
            code: Python code to execute

        Calls within one request share a session; the caller stops it.

        Returns:
            JSON string with execution result. On failure the details are
            logged server-side and only a generic error plus a correlation id
            is returned.
        """
        try:
            client = self._get_code_interpreter_client()
            response = client.invoke(
                "executeCode",
                {"code": code, "language": "python", "clearContext": False},
            )

            results = []
            for event in response["stream"]:
                if "result" in event:
                    results.append(event["result"])

            return (
                json.dumps(results, indent=2)
                if results
                else json.dumps({"error": "No results returned"}, indent=2)
            )
        except Exception:
            correlation_id = uuid.uuid4().hex
            logger.exception(
                "Code execution failed (correlation_id=%s)", correlation_id
            )
            return json.dumps(
                {"error": "Code execution failed", "correlation_id": correlation_id},
                indent=2,
            )
