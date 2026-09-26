"""
Run the NetCopilot replay evals against the configured LLM.

    .venv/bin/python -m evals.run_evals [--case ID ...] [--verbose] [--json out.json]

Uses the LLM settings from the environment / settings.db (LLM_PROVIDER, model,
API key). Every case makes real LLM calls, so each run costs tokens; the token
totals are printed at the end. No device, Jira, Slack or syslog is contacted.
Exit code is non-zero when any case fails, so it can gate CI.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", action="append", help="Run only this case id (repeatable)")
    parser.add_argument("--verbose", action="store_true", help="Print each reply")
    parser.add_argument("--json", help="Write the summary as JSON to this file")
    args = parser.parse_args()

    # Isolate runtime state: evals never write to the real memory/audit DBs.
    os.environ.setdefault("NETCOPILOT_DATA_DIR", tempfile.mkdtemp(prefix="netcopilot-evals-"))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root)
    os.chdir(root)

    import app as app_module
    from evals.harness import load_cases, run_case, summarize, to_json

    if not app_module.get_llm_adapter():
        print("No LLM configured (set LLM_PROVIDER and its API key).", file=sys.stderr)
        return 2

    app_module.audit.init_audit_db()
    app_module._init_memory_db()
    cases = load_cases(args.case)
    results = []
    for case in cases:
        result = asyncio.run(run_case(case, app_module))
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {case['id']:<28} {result.seconds:>5}s  tools={','.join(result.tools_called) or '-'}")
        for failure in result.failures:
            print(f"        - {failure}")
        if args.verbose:
            print("        reply:", result.reply.replace("\n", "\n               "))

    summary = summarize(results)
    print(f"\nScore: {summary['passed']}/{summary['total']}  "
          f"tokens: {summary['prompt_tokens']} prompt + {summary['completion_tokens']} completion")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(to_json(results))
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
