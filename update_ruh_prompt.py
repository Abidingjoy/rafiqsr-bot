"""
One-off: push updated SYSTEM_PROMPT from setup_pregnancy.py into the existing Ruh agent.
Run locally:
    $env:ANTHROPIC_API_KEY="sk-ant-..."
    python update_ruh_prompt.py
"""
import os
from dotenv import load_dotenv
load_dotenv()
from anthropic import Anthropic

from setup_pregnancy import SYSTEM_PROMPT

client = Anthropic()
AGENT_ID = os.environ["PREG_AGENT_ID"]

def main():
    agent = client.beta.agents.retrieve(AGENT_ID)
    print(f"Current version: {agent.version}")

    updated = client.beta.agents.update(
        AGENT_ID,
        version=agent.version,
        system=SYSTEM_PROMPT,
    )
    print(f"✅ Updated → version {updated.version}")
    print(f"New system prompt length: {len(SYSTEM_PROMPT)}")

if __name__ == "__main__":
    main()
