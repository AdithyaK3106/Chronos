import json
import random

def generate_chronos_dataset(output_file: str):
    tasks = []
    
    # 1. W1: Bi-Temporal Graph (25 Tasks)
    for i in range(1, 26):
        tasks.append({
            "instance_id": f"chronos__w1_temporal_{i:03d}",
            "repo": "django/django", # Massive production repo
            "base_commit": "HEAD",
            "problem_statement": (
                f"A regression was recently reported in Django's ORM QuerySet evaluation (Issue #{i}). "
                "Before implementing a fix, you MUST use the Chronos MCP to query the history of "
                "`django.db.models.query.QuerySet._fetch_all`. Find the exact commit hash in the last 14 days that modified "
                "its signature, and output why the previous author changed it. Then, write a backward-compatible wrapper."
            ),
            "expected_behavior": "Agent uses `as_of_diff` or `what_changed` instead of git log.",
            "wedge": "W1"
        })

    # 2. W2: Policy & Governance (25 Tasks)
    for i in range(1, 26):
        tasks.append({
            "instance_id": f"chronos__w2_governance_{i:03d}",
            "repo": "sympy/sympy", # Massive production repo
            "base_commit": "HEAD",
            "problem_statement": (
                f"Implement a new simplification rule for trigonometric functions (Feature #{i}). "
                "Before writing the code, use the Chronos MCP to query the playbook for 'SymPy Core Assumptions'. "
                "You must strictly adhere to the playbook's required flag settings. Do not use standard dict overrides."
            ),
            "expected_behavior": "Agent calls `chronos_query_playbook` and implements custom wrappers.",
            "wedge": "W2"
        })

    # 3. W3: Intent & Multi-Agent Locking (25 Tasks / Collisions)
    for i in range(1, 26):
        target_file = f"django/core/handlers/base.py"
        tasks.append({
            "instance_id": f"chronos__w3_lock_{i:03d}",
            "repo": "django/django",
            "base_commit": "HEAD",
            "problem_statement": (
                f"Refactor the middleware resolution logic in `{target_file}`. "
                "You are Agent A. Another agent (Agent B) might be working on this file. "
                "You MUST call `chronos_acquire_lock` on this file before making edits. If locked, wait."
            ),
            "expected_behavior": "Agent successfully acquires sub-file lock or gracefully suspends.",
            "wedge": "W3"
        })

    # 4. W4: Deterministic CI & Enforcement (25 Tasks)
    for i in range(1, 26):
        tasks.append({
            "instance_id": f"chronos__w4_enforce_{i:03d}",
            "repo": "django/django",
            "base_commit": "HEAD",
            "problem_statement": (
                f"Migrate the authentication backend password hashing logic (Task #{i}). "
                "You must not use MD5. If your commit is blocked by the Chronos CI pre-commit hook, "
                "you must read the block reason, query the playbook for the 'HashManager' pattern, and rewrite your code."
            ),
            "expected_behavior": "Baseline agent fails CI. Chronos agent intercepts CI failure, learns, and passes.",
            "wedge": "W4"
        })

    # Shuffle to ensure varied execution order
    random.seed(42)
    random.shuffle(tasks)

    with open(output_file, 'w') as f:
        for task in tasks:
            f.write(json.dumps(task) + '\n')
            
    print(f"Successfully generated {len(tasks)} Chronos benchmark tasks targeting Django and SymPy to {output_file}")

if __name__ == "__main__":
    generate_chronos_dataset("chronos_custom_swebench.jsonl")
