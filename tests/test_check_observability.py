"""The live observability gate must inspect every Logs API page."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_observability


class FakePaginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self):
        return iter(self.pages)


class FakeLogs:
    def __init__(self):
        self.pages = {
            "describe_delivery_sources": [
                {
                    "deliverySources": [
                        {"name": "agentcore-workshop-dev-gateway-app-logs"}
                    ]
                },
                {
                    "deliverySources": [
                        {"name": "agentcore-workshop-dev-runtime-app-logs"}
                    ]
                },
            ],
            "describe_deliveries": [
                {
                    "deliveries": [
                        {
                            "deliverySourceName": (
                                "agentcore-workshop-dev-gateway-app-logs"
                            )
                        }
                    ]
                },
                {
                    "deliveries": [
                        {
                            "deliverySourceName": (
                                "agentcore-workshop-dev-runtime-app-logs"
                            )
                        }
                    ]
                },
            ],
        }

    def get_paginator(self, operation):
        return FakePaginator(self.pages[operation])


def test_log_delivery_check_reads_every_page(monkeypatch, capsys):
    monkeypatch.setattr(
        check_observability.boto3, "client", lambda *args, **kwargs: FakeLogs()
    )
    check_observability.check_log_deliveries()
    assert "PASS: 2 vended log deliveries active" in capsys.readouterr().out
