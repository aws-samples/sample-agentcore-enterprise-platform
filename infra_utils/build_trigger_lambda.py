"""Build trigger handlers for the CDK custom-resource Provider framework.

on_event starts the CodeBuild build and returns its id as the physical id;
is_complete is polled by the Provider (asynchronously, no Lambda sitting in a
sleep loop) until the build finishes. The Provider's total timeout, not the
15-minute Lambda ceiling, bounds the wait, so it can cover the 30-minute
CodeBuild timeout.

Stacks deployed before the Provider existed hold a custom resource whose
service token is this function itself. That resource is replaced under a new
logical id, and CloudFormation then sends its Delete straight here (with a
real pre-signed ResponseURL; the Provider framework passes "..."), so on_event
answers it directly instead of leaving the stack cleanup hanging.
"""

import functools
import json
import urllib.request

import boto3

FAILED_STATUSES = ("FAILED", "FAULT", "STOPPED", "TIMED_OUT")


@functools.lru_cache(maxsize=1)
def _client():
    return boto3.client("codebuild")


def _respond_legacy_delete(event):
    body = json.dumps(
        {
            "Status": "SUCCESS",
            "Reason": "Legacy build trigger replaced by the Provider resource",
            "PhysicalResourceId": event["PhysicalResourceId"],
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
        }
    ).encode()
    req = urllib.request.Request(
        event["ResponseURL"],
        data=body,
        method="PUT",
        headers={"Content-Type": "", "Content-Length": str(len(body))},
    )
    urllib.request.urlopen(req, timeout=30)  # nosec B310 — https checked by caller


def on_event(event, context):
    if event["RequestType"] == "Delete":
        if event.get("ResponseURL", "").startswith("https://"):
            _respond_legacy_delete(event)
        return {"PhysicalResourceId": event["PhysicalResourceId"]}
    props = event["ResourceProperties"]
    build = _client().start_build(
        projectName=props["ProjectName"],
        environmentVariablesOverride=props.get("EnvironmentOverrides", []),
    )
    return {"PhysicalResourceId": build["build"]["id"]}


def is_complete(event, context):
    if event["RequestType"] == "Delete":
        return {"IsComplete": True}
    build_id = event["PhysicalResourceId"]
    resp = _client().batch_get_builds(ids=[build_id])
    status = resp["builds"][0]["buildStatus"]
    if status == "SUCCEEDED":
        return {"IsComplete": True, "Data": {"BuildId": build_id}}
    if status in FAILED_STATUSES:
        raise RuntimeError(f"Build {build_id} {status}")
    return {"IsComplete": False}
