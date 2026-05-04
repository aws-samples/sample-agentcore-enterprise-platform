"""Simulated EC2 agent — this represents the customer's existing agent before migration.

This is a simple Strands agent running on EC2 with no managed infrastructure.
The workshop will migrate this to AgentCore Runtime.
"""
import os
from strands import Agent
from strands.models.bedrock import BedrockModel


def create_agent():
    model = BedrockModel(
        model_id="anthropic.claude-sonnet-4-20250514",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    agent = Agent(
        model=model,
        system_prompt="You are a helpful assistant for ACME Corp. You help employees with research and code tasks.",
    )
    return agent


if __name__ == "__main__":
    agent = create_agent()
    print("ACME Corp Agent running on EC2 (no managed infra)")
    print("Type 'quit' to exit\n")
    while True:
        user_input = input("You: ")
        if user_input.lower() == "quit":
            break
        response = agent(user_input)
        print(f"Agent: {response}\n")
