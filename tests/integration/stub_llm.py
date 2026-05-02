"""Tiny scripted LLM stub for manual ansible_ai E2E smoke.

Speaks Anthropic Messages API shape. Two-turn dialog hard-coded for the
smoke playbook:
  turn 1: tool_use run_cmd ['uname', '-r']
  turn 2: tool_use done with summary

Usage:
  python tests/integration/stub_llm.py 8765 &
  ANSIBLE_COLLECTIONS_PATH=$PWD/.. ANTHROPIC_API_KEY=stub \\
    ansible-playbook tests/integration/playbook_localhost_smoke.yml \\
      -e provider=claude -e endpoint=http://127.0.0.1:8765 \\
      -e api_key=stub -e model=stub-claude

Not wired into CI: real ansible-playbook in CI hits macOS fork-safety,
port-collision, and stub-maintenance issues. The orchestrator-layer E2E
in tests/eval/ already exercises the full tool-use loop deterministically.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = {"call": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a, **_k):
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        STATE["call"] += 1

        if STATE["call"] == 1:
            payload = {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "run_cmd",
                        "input": {"argv": ["uname", "-r"], "reason": "fetch kernel"},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 50, "output_tokens": 30},
            }
        else:
            payload = {
                "id": "msg_2",
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_2",
                        "name": "done",
                        "input": {
                            "summary": "Kernel version reported via uname -r.",
                            "reason": "have kernel",
                        },
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 60, "output_tokens": 40},
            }

        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
