"""Build trigger Lambda handler — starts CodeBuild and waits for completion."""
import json
import time
import urllib.request
import boto3

codebuild = boto3.client("codebuild")


def handler(event, context):
    response_url = event["ResponseURL"]
    try:
        if event["RequestType"] == "Delete":
            return _send(response_url, event, "SUCCESS", {})

        project_name = event["ResourceProperties"]["ProjectName"]
        env_overrides = event["ResourceProperties"].get("EnvironmentOverrides", [])

        build = codebuild.start_build(
            projectName=project_name,
            environmentVariablesOverride=env_overrides,
        )
        build_id = build["build"]["id"]

        # Poll until complete (max ~14 min)
        for _ in range(28):
            time.sleep(30)
            resp = codebuild.batch_get_builds(ids=[build_id])
            status = resp["builds"][0]["buildStatus"]
            if status == "SUCCEEDED":
                return _send(response_url, event, "SUCCESS", {"BuildId": build_id})
            if status in ("FAILED", "FAULT", "STOPPED", "TIMED_OUT"):
                return _send(response_url, event, "FAILED", {}, f"Build {status}")

        return _send(response_url, event, "FAILED", {}, "Build timed out")
    except Exception as e:
        return _send(response_url, event, "FAILED", {}, str(e))


def _send(url, event, status, data, reason=""):
    body = json.dumps({
        "Status": status,
        "Reason": reason or "See CloudWatch",
        "PhysicalResourceId": event.get("PhysicalResourceId", event["LogicalResourceId"]),
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data,
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ""}, method="PUT")
    urllib.request.urlopen(req)
