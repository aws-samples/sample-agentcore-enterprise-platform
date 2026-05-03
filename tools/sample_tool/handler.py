# Adapted from fullstack-solution-template-for-agentcore
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import json
import logging
from collections import Counter

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def analyze_text(text: str, n: int = 5) -> str:
    """
    Analyzes text to count words and find most frequent characters.

    Args:
        text: Input text to analyze
        n: Number of most frequent characters to return

    Returns:
        Formatted analysis results as string
    """
    word_count = len(text.split())
    char_counter = Counter(char.lower() for char in text if char != " ")
    top_chars = char_counter.most_common(n)

    result = "Text analysis results:\n"
    result += f"Word count: {word_count}\n"
    result += f"Top {n} most frequent characters:\n"
    for char, count in top_chars:
        result += f"  '{char}': {count}\n"

    return result


def handler(event, context):
    """
    Text analysis tool Lambda function for AgentCore Gateway.

    INPUT FORMAT:
    - event: Contains tool arguments directly (not wrapped in HTTP body)
    - context.client_context.custom['bedrockAgentCoreToolName']: Full tool name with target prefix

    OUTPUT FORMAT:
    - Return object with 'content' array containing response data

    Args:
        event (dict): Tool arguments passed directly from gateway
        context: Lambda context with AgentCore metadata in client_context.custom

    Returns:
        dict: Response object with 'content' array or 'error' string
    """
    logger.info(f"Received event: {json.dumps(event)}")

    try:
        delimiter = "___"
        original_tool_name = context.client_context.custom["bedrockAgentCoreToolName"]
        tool_name = original_tool_name[
            original_tool_name.index(delimiter) + len(delimiter) :
        ]

        logger.info(f"Processing tool: {tool_name}")

        if tool_name == "text_analysis_tool":
            text = event.get("text", "")
            N = event.get("N", 5)
            result = analyze_text(text, N)
            return {"content": [{"type": "text", "text": result}]}
        else:
            logger.error(f"Unexpected tool name: {tool_name}")
            return {
                "error": f"This Lambda only supports 'text_analysis_tool', received: {tool_name}"
            }

    except Exception as e:
        logger.error(f"Error processing request: {str(e)}")
        return {"error": f"Internal server error: {str(e)}"}
