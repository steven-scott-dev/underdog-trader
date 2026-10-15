"""Vercel serverless entrypoint: GET /api/trade runs one trading pass."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from http.server import BaseHTTPRequestHandler  # noqa: E402

from trader.core import run_pass  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Vercel sends `Authorization: Bearer <CRON_SECRET>` on cron runs
        # when CRON_SECRET is set. Enforce it so strangers can't fire trades.
        required = os.environ.get("CRON_SECRET", "")
        if required:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {required}":
                self._send(401, {"ok": False, "error": "unauthorized"})
                return
        try:
            result = run_pass()
            self._send(200, result)
        except Exception as e:  # never leak a stack to the caller
            self._send(500, {"ok": False,
                             "error": f"{type(e).__name__}: {e}"})

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep Vercel logs clean
        pass
