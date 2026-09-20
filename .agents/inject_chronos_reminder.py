import sys
import json

def main():
    try:
        sys.stdin.read()
        response = {
            "injectSteps": [
                {
                    "ephemeralMessage": (
                        "?? CHRONOS CONTEXT PROTOCOL ??\n"
                        "You are operating in a Chronos-governed workspace. Generic search tools waste tokens.\n"
                        "1. RESEARCH: You MUST use 'as_of_callers', 'as_of_callees', and 'as_of_impact' to understand dependencies.\n"
                        "2. STANDARDS: Use 'chronos_query_playbook' to look up architectural rules.\n"
                        "3. GOVERNANCE: Always acquire intent locks ('chronos_acquire_lock') before modifying files."
                    )
                }
            ]
        }
        print(json.dumps(response))
    except Exception:
        print("{}")

if __name__ == "__main__":
    main()
