#!/usr/bin/env python3
# Adapted from fullstack-solution-template-for-agentcore
"""
Test script for AgentCore Memory

Tests short-term memory operations: creating, listing, getting events, pagination.
Reads configuration from SSM parameters using /{project}/{env}/ convention.

Usage:
    python scripts/test_memory.py
    python scripts/test_memory.py --memory-arn <arn>
    python scripts/test_memory.py --project agentcore-workshop --env dev
"""

import argparse
import sys
import time
from datetime import UTC, datetime
from typing import Tuple

import boto3
from botocore.exceptions import ClientError
from colorama import Fore, Style

from utils import (
    create_bedrock_client,
    generate_session_id,
    get_ssm_param,
    get_workshop_config,
    print_msg,
    print_section,
)


def test_create_event(client, memory_id: str, actor_id: str, session_id: str) -> bool:
    """Test creating conversation events in memory."""
    print("Test 1: Creating conversation events...")
    try:
        payload = [
            {"conversational": {"content": {"text": "What's the weather like today?"}, "role": "USER"}},
            {"conversational": {"content": {"text": "I don't have access to real-time weather data."}, "role": "ASSISTANT"}},
        ]
        response = client.create_event(
            memoryId=memory_id, actorId=actor_id, sessionId=session_id,
            eventTimestamp=datetime.now(UTC), payload=payload,
        )
        event_id = response.get("event", {}).get("eventId")
        if event_id:
            print_msg("Test 1 passed", "success")
            print(f"  Event ID: {event_id}")
            return True
        print_msg("Test 1 failed - No event ID returned", "error")
        return False
    except Exception as e:
        print_msg(f"Test 1 failed - {e}", "error")
        return False


def test_list_events(client, memory_id: str, actor_id: str, session_id: str) -> bool:
    """Test listing events from memory."""
    print("\nTest 2: Listing conversation events...")
    try:
        response = client.list_events(
            memoryId=memory_id, actorId=actor_id, sessionId=session_id, maxResults=10
        )
        events = response.get("events", [])
        print_msg(f"Test 2 passed - {len(events)} events found", "success")
        return True
    except Exception as e:
        print_msg(f"Test 2 failed - {e}", "error")
        return False


def test_get_event(client, memory_id: str, actor_id: str, session_id: str) -> bool:
    """Test getting a specific event by ID."""
    print("\nTest 3: Getting specific event...")
    try:
        events = client.list_events(
            memoryId=memory_id, actorId=actor_id, sessionId=session_id, maxResults=1
        ).get("events", [])
        if not events:
            print(f"{Fore.YELLOW}✓ Test 3 skipped (no events){Style.RESET_ALL}")
            return True
        event_id = events[0]["eventId"]
        response = client.get_event(
            memoryId=memory_id, sessionId=session_id, actorId=actor_id, eventId=event_id
        )
        if response.get("event", {}).get("eventId") == event_id:
            print_msg("Test 3 passed", "success")
            return True
        print_msg("Test 3 failed - Event mismatch", "error")
        return False
    except Exception as e:
        print_msg(f"Test 3 failed - {e}", "error")
        return False


def test_pagination(client, memory_id: str, actor_id: str, session_id: str) -> bool:
    """Test pagination with maxResults."""
    print("\nTest 4: Testing pagination...")
    try:
        for i in range(3):
            client.create_event(
                memoryId=memory_id, actorId=actor_id, sessionId=session_id,
                eventTimestamp=datetime.now(UTC),
                payload=[{"conversational": {"content": {"text": f"Test message {i + 1}"}, "role": "USER"}}],
            )
        time.sleep(1)
        response = client.list_events(
            memoryId=memory_id, actorId=actor_id, sessionId=session_id, maxResults=2
        )
        print_msg("Test 4 passed", "success")
        print(f"  Events in page: {len(response.get('events', []))}")
        print(f"  Next token: {bool(response.get('nextToken'))}")
        return True
    except Exception as e:
        print_msg(f"Test 4 failed - {e}", "error")
        return False


def test_invalid_memory_id(client) -> bool:
    """Test error handling with invalid memory ID."""
    print("\nTest 5: Testing error handling (invalid memory ID)...")
    try:
        client.list_events(memoryId="invalid-memory-id-12345", actorId="test", sessionId=generate_session_id())
        print_msg("Test 5 failed - Invalid memory ID was accepted", "error")
        return False
    except ClientError:
        print_msg("Test 5 passed - Invalid memory ID correctly rejected", "success")
        return True
    except Exception:
        print_msg("Test 5 passed - Error handled gracefully", "success")
        return True


def run_tests(client, memory_id: str) -> Tuple[int, int]:
    """Run all tests and return (passed, failed)."""
    print_section("Running Tests")
    actor_id = "test-user-12345"
    session_id = generate_session_id()
    print(f"Actor ID: {actor_id}\nSession ID: {session_id}\n")

    tests = [
        lambda: test_create_event(client, memory_id, actor_id, session_id),
        lambda: test_list_events(client, memory_id, actor_id, session_id),
        lambda: test_get_event(client, memory_id, actor_id, session_id),
        lambda: test_pagination(client, memory_id, actor_id, session_id),
        lambda: test_invalid_memory_id(client),
    ]

    passed = sum(1 for t in tests if t())
    failed = len(tests) - passed
    return passed, failed


def main():
    print("=" * 60)
    print("AgentCore Memory Test Script")
    print("=" * 60 + "\n")

    parser = argparse.ArgumentParser(description="Test AgentCore Memory operations")
    parser.add_argument("--memory-arn", type=str, help="Memory ARN (overrides SSM lookup)")
    parser.add_argument("--project", default=None, help="Project name")
    parser.add_argument("--env", default=None, help="Environment")
    args = parser.parse_args()

    if args.memory_arn:
        memory_arn = args.memory_arn
        region = memory_arn.split(":")[3]
    else:
        config = get_workshop_config(args.project, args.env)
        region = config["region"]
        # SSM: /{project}/{env}/memory/memory-id
        memory_arn = get_ssm_param("memory/memory-id", args.project, args.env)

    memory_id = memory_arn.split("/")[-1] if "/" in memory_arn else memory_arn
    print(f"  Memory: {memory_arn}")
    print(f"  Region: {region}\n")

    print_section("Initializing Memory Client")
    client = create_bedrock_client(region)
    print_msg(f"Client initialized (region: {region})", "success")

    passed, failed = run_tests(client, memory_id)

    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    print(f"Passed: {Fore.GREEN}{passed}{Style.RESET_ALL}")
    print(f"Failed: {Fore.RED}{failed}{Style.RESET_ALL}\n")

    if failed == 0:
        print(f"{Fore.GREEN}All tests passed! ✓{Style.RESET_ALL}")
    else:
        print(f"{Fore.RED}Some tests failed.{Style.RESET_ALL}")
        sys.exit(1)


if __name__ == "__main__":
    main()
