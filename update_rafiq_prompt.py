"""
One-off: push updated SYSTEM_PROMPT from setup.py into the existing Rafiq agent.
Run locally after editing setup.py:
    python update_rafiq_prompt.py
"""
import os
from dotenv import load_dotenv
load_dotenv()
from anthropic import Anthropic

from setup import SYSTEM_PROMPT

client = Anthropic()
AGENT_ID = os.environ["AGENT_ID"]

def main():
    agent = client.beta.agents.retrieve(AGENT_ID)
    print(f"Current version: {agent.version}")
    print(f"Current model: {agent.model}")

    updated = client.beta.agents.update(
        AGENT_ID,
        version=agent.version,
        system=SYSTEM_PROMPT,
    )
    print(f"✅ Updated → version {updated.version}")
    print("New system prompt length:", len(SYSTEM_PROMPT))

if __name__ == "__main__":
    main()
