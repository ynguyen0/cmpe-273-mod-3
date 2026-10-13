import argparse
import json
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common import available, validate


def handler_for(args):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, payload):
            data = json.dumps(payload).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path == "/health":
                self.reply(200, {"status": "ok", "service": args.service})
            else:
                self.reply(404, {"error": "not_found"})

        def do_POST(self):
            path = "/orders" if args.service == "order" else "/inventory/check"
            if self.path != path:
                return self.reply(404, {"error": "not_found"})
            if self.headers.get_content_type() != "application/json":
                return self.reply(415, {"error": "expected_application_json"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("JSON body must be between 1 and 4096 bytes")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("JSON body must be an object")
                item_id, quantity = body.get("item_id"), body.get("quantity")
                validate(item_id, quantity)
            except (ValueError, UnicodeDecodeError) as exc:
                return self.reply(400, {"error": "invalid_request", "message": str(exc)})

            if args.service == "inventory":
                time.sleep(args.delay)
                if not available(item_id, quantity):
                    return self.reply(409, {"error": "item_unavailable"})
                return self.reply(200, {"available": True, "item_id": item_id, "quantity": quantity})

            request = Request(
                args.inventory_url + "/inventory/check",
                data=json.dumps({"item_id": item_id, "quantity": quantity}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            try:
                with urlopen(request, timeout=args.timeout) as response:
                    result = json.load(response)
                    if result.get("available") is not True:
                        raise ValueError("invalid inventory response")
            except HTTPError as exc:
                if exc.code == 409:
                    return self.reply(409, {"error": "item_unavailable"})
                return self.reply(502, {"error": "inventory_error"})
            except (TimeoutError, socket.timeout):
                return self.reply(504, {"error": "inventory_timeout"})
            except URLError as exc:
                if isinstance(exc.reason, TimeoutError):
                    return self.reply(504, {"error": "inventory_timeout"})
                return self.reply(503, {"error": "inventory_unreachable"})
            except (ValueError, OSError, AttributeError):
                return self.reply(502, {"error": "inventory_error"})
            self.reply(201, {"status": "accepted", "item_id": item_id, "quantity": quantity})

    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=["order", "inventory"])
    parser.add_argument("--port", type=int)
    parser.add_argument("--inventory-url", default="http://127.0.0.1:8001")
    parser.add_argument("--timeout", type=float, default=0.5)
    parser.add_argument("--delay", type=float, default=0)
    args = parser.parse_args()
    if args.timeout <= 0 or args.delay < 0:
        parser.error("timeout must be positive and delay nonnegative")
    port = args.port or (8000 if args.service == "order" else 8001)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(args))
    print(f"REST {args.service} listening on {port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
