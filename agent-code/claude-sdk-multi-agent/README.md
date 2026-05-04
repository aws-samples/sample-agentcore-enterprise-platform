# Adapted from fullstack-solution-template-for-agentcore
# Claude Agent SDK Multi-Agent Pattern

This pattern integrates Anthropic's Claude Agent SDK with Amazon Bedrock AgentCore, providing Code Interpreter access via an in-process MCP server, subagent delegation via the Task tool, and Gateway tool integration. For a simpler single-agent version without subagents, see the `claude-agent-sdk-single-agent` pattern.

## Features

- **Claude Agent SDK**: Uses Anthropic's official agent SDK (`ClaudeSDKClient`) for agentic workflows on Bedrock
- **Code Interpreter**: Execute Python code, bash commands, and file operations via an in-process MCP server
- **Subagent Spawning**: Delegate focused subtasks to a specialized `code-analyst` subagent via the Task tool
- **Gateway Integration**: Access Lambda-based tools through AgentCore Gateway (MCP protocol with OAuth2 auth)
- **Session Management**: Resume conversations across requests via `claude_session_id`
- **Secure Identity**: User identity extracted from validated JWT token (`RequestContext`), not from payload

## Architecture

```
User Request
    |
BedrockAgentCoreApp (agent.py)
    |
ClaudeSDKClient (Opus model via Bedrock)
    |
    +-- Code Interpreter MCP (in-process)
    |     execute_code, execute_command, write_files, read_files
    |
    +-- Gateway MCP (HTTP, optional)
    |     Lambda-based tools via AgentCore Gateway
    |
    +-- Task tool (subagent spawning)
          code-analyst (Sonnet) — analyze output, debug errors
```

## File Structure

```
agent-code/claude-sdk-multi-agent/
├── agent.py                  # Main entrypoint (BedrockAgentCoreApp)
├── agents/
│   └── subagents.py          # Subagent definitions (code-analyst)
├── code_int_mcp/
│   ├── server.py             # MCP server with @tool definitions
│   ├── client.py             # boto3 wrapper for AgentCore Code Interpreter API
│   └── models.py             # Pydantic result model
├── requirements.txt          # Python dependencies
├── Dockerfile                # Container build (Python 3.11 + Node.js + claude-code CLI)
└── README.md
```

## Deployment

```bash
cd workshop-cdk
./scripts/deploy.sh deploy
```

**Note**: This pattern requires Docker deployment because it needs Node.js and the `@anthropic-ai/claude-code` npm package installed at build time.
