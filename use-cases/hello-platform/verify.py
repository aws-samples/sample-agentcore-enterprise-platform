#!/usr/bin/env python3
"""Verify hello-platform: the parameter it publishes must exist and carry a
real gateway URL. Exits non-zero on any failed claim — a verify that cannot
fail is not a verify (this is a contribution requirement, not a suggestion).

Usage: python use-cases/hello-platform/verify.py
Respects PROJECT_NAME / ENVIRONMENT / AWS_REGION.
"""

import os
import sys

import boto3

project = os.environ.get("PROJECT_NAME", "agentcore-workshop")
env = os.environ.get("ENVIRONMENT", "dev")
name = f"/{project}/{env}/use-cases/hello-platform/gateway-seen"

ssm = boto3.client("ssm")
try:
    value = ssm.get_parameter(Name=name)["Parameter"]["Value"]
except ssm.exceptions.ParameterNotFound:
    sys.exit(f"FAIL: {name} not found — is the use case deployed?")

if "https://" not in value:
    sys.exit(f"FAIL: {name} holds no gateway URL: {value!r}")

print(f"OK: {name} = {value}")
