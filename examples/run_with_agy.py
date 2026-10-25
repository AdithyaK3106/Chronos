"""Run an OpenHands agent governed by Chronos using your local AGY session (zero API keys needed)."""

from openhands.sdk.llm import Message, TextContent
from chronos.context.budgeter import assemble_context
from chronos.core.config import ChronosConfig
from chronos.security.permissions import set_permissions
from runtime import ChronosAgentRuntime, AgyLLM

def main():
    print("⏳ Initializing Chronos Governance & OpenHands Harness with AGY...")
    config = ChronosConfig.load()
    runtime = ChronosAgentRuntime(config=config)

    # 1. Configure agent permissions (least-privilege)
    agent_id = "chronos_agy_agent"
    set_permissions(
        runtime.con,
        agent_id=agent_id,
        manifest={
            "allowed_paths": ["src/**", "tests/**", "docs/**"],
            "denied_paths": [".env*", "**/secrets/**"],
            "read_only": False,
        },
    )

    # 2. Assemble bi-temporal context from the repository
    ctx = assemble_context(["authenticate_user"], runtime.graph_store, token_budget=1500)
    print(f"\n[Chronos Context Assembled]: {ctx['token_count']} estimated tokens")

    # 3. Create AgyLLM (routes directly through your local authenticated agy session)
    llm = AgyLLM(model="gemini-3.8-flash-high")

    # 4. Create OpenHands Agent
    agent = runtime.create_agent(llm=llm, agent_id=agent_id)
    print(f"✅ OpenHands Agent ready with model: {llm.model}")

    # 5. Ask the agent a task query
    prompt = f"Given this codebase context:\n{ctx['text']}\n\nSummarize the callers and security implications."
    print("\n[Querying AGY via OpenHands...]")
    resp = llm.completion([Message(role="user", content=[TextContent(text=prompt)])])

    print("\n--- AGY Response ---")
    print(resp.message.content[0].text)

if __name__ == "__main__":
    main()
