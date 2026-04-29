"""Research Agent — A2A sub-agent for information retrieval and analysis."""
import os
from strands import Agent
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """You are a research agent. You help with:
- Gathering and synthesizing information
- Analyzing documents and data
- Providing summaries and insights
- Answering factual questions with citations"""


@app.entrypoint
async def invoke(payload=None):
    query = payload.get("prompt", "Hello!") if payload else "Hello!"
    agent = Agent(
        system_prompt=SYSTEM_PROMPT,
        name="ResearchAgent",
        model_id=os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0"),
    )
    response = agent(query)
    return {"status": "success", "response": response.message["content"][0]["text"]}


if __name__ == "__main__":
    app.run()
