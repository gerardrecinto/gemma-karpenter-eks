"""A stand-in for the vLLM server, for the local cluster run only.

It is not vLLM and serves no model. It exposes the same paths the manifests and
the eval script use, so the cluster plumbing can be tested without a GPU:

  GET  /health                 readiness and startup probes
  GET  /metrics                vllm:num_requests_running and vllm:num_requests_waiting
  POST /v1/chat/completions    a canned answer, so eval/compare.py can run
  POST /load {"waiting": N}    set the queue gauge, to simulate load for KEDA
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

state = {"running": 0, "waiting": 0}
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, "ok", "text/plain")
        elif self.path == "/metrics":
            with lock:
                body = (
                    "# TYPE vllm:num_requests_running gauge\n"
                    f"vllm:num_requests_running {state['running']}\n"
                    "# TYPE vllm:num_requests_waiting gauge\n"
                    f"vllm:num_requests_waiting {state['waiting']}\n"
                )
            self._send(200, body, "text/plain; version=0.0.4")
        else:
            self._send(404, "{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        if self.path == "/load":
            with lock:
                state["waiting"] = int(json.loads(raw).get("waiting", 0))
            self._send(200, json.dumps(state))
        elif self.path == "/v1/chat/completions":
            model = json.loads(raw).get("model", "base")
            reply = f"stub answer from {model}"
            self._send(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": reply}}]}))
        else:
            self._send(404, "{}")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
