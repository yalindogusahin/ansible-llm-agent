"""Tiny scripted LLM stub for ansible_ai integration tests.

Speaks Anthropic Messages API shape. Stateless: the response shape is
chosen from the request body, not from a server-side counter, so concurrent
multi-host playbooks each see a coherent two-turn dialog without the calls
interleaving and confusing each other.

  request has no tool_result block  →  emit tool_use run_cmd [uname -r]
  request has    tool_result block  →  emit tool_use done with summary

Usage:
  python tests/integration/stub_llm.py 8765 &
  ANSIBLE_COLLECTIONS_PATH=$PWD/.. ANTHROPIC_API_KEY=stub \\
    ansible-playbook tests/integration/playbook_localhost_smoke.yml \\
      -e provider=claude -e endpoint=http://127.0.0.1:8765 \\
      -e api_key=stub -e model=stub-claude
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn


def _first_turn_payload() -> dict:
    return {
        "id": "msg_first",
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


def _done_payload() -> dict:
    return {
        "id": "msg_done",
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "tu_done",
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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a, **_k):
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        # Statelessness: classify by what the conversation already contains.
        # If the request includes a tool_result, we're past the first turn
        # and should close out with `done`.
        payload = _done_payload() if b'"tool_result"' in body else _first_turn_payload()
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
