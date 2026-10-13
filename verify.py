import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import grpc
import inventory_pb2 as pb
import inventory_pb2_grpc as rpc

ROOT = Path(__file__).resolve().parent


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start(script, service, port, *options):
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / script), service, "--port", str(port), *options],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError(f"{service} exited: {proc.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return proc
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"{service} did not start")
    except BaseException:
        stop(proc)
        raise


def stop(proc):
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def rest(port, item="book", quantity=2, path="/orders"):
    req = Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps({"item_id": item, "quantity": quantity}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        response = urlopen(req, timeout=3)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, json.load(response)


def check_rest():
    ip, op = free_port(), free_port()
    inventory = order = None
    try:
        inventory = start("rest_service.py", "inventory", ip)
        order = start("rest_service.py", "order", op, "--inventory-url", f"http://127.0.0.1:{ip}")
        status, body = rest(ip, path="/inventory/check")
        assert status == 200 and body["available"] is True
        status, body = rest(op)
        assert status == 201 and body == {"status": "accepted", "item_id": "book", "quantity": 2}
        print(f"REST success: HTTP {status} {json.dumps(body)}", flush=True)
        for item, quantity, expected in [("missing", 1, 409), ("book", 11, 409), ("book", 0, 400), ("book", True, 400)]:
            assert rest(op, item, quantity)[0] == expected
        print("REST unavailable/insufficient: HTTP 409; invalid quantity: HTTP 400", flush=True)
        stop(inventory)
        inventory = start("rest_service.py", "inventory", ip, "--delay", "1.5")
        started = time.monotonic()
        status, body = rest(op)
        elapsed = time.monotonic() - started
        assert status == 504 and body["error"] == "inventory_timeout" and elapsed < 1.3
        print(f"REST delayed inventory (1.5s), timeout (0.5s): HTTP {status} {json.dumps(body)} in {elapsed:.2f}s", flush=True)
        assert order.poll() is None
        with urlopen(f"http://127.0.0.1:{op}/health", timeout=2) as response:
            assert response.status == 200
            print(f"REST after failure: HTTP 200 {response.read().decode()}; original Order PID {order.pid} alive", flush=True)
        stop(inventory)
        inventory = start("rest_service.py", "inventory", ip)
        status, body = rest(op)
        assert status == 201 and order.poll() is None
        print(f"REST recovery: HTTP {status} {json.dumps(body)}; same Order PID {order.pid}", flush=True)
    finally:
        stop(order)
        stop(inventory)


def check_grpc():
    ip, op = free_port(), free_port()
    inventory = order = None
    try:
        inventory = start("grpc_service.py", "inventory", ip)
        order = start("grpc_service.py", "order", op, "--inventory-target", f"127.0.0.1:{ip}")
        with grpc.insecure_channel(f"127.0.0.1:{op}") as channel:
            grpc.channel_ready_future(channel).result(timeout=5)
            stub = rpc.OrderServiceStub(channel)

            def call(item="book", quantity=2):
                return stub.PlaceOrder(pb.OrderRequest(item_id=item, quantity=quantity), timeout=3)

            def success():
                reply = call()
                assert reply.status == "accepted" and reply.item_id == "book" and reply.quantity == 2
                return json.dumps({"status": reply.status, "item_id": reply.item_id, "quantity": reply.quantity})

            print(f"gRPC success: OK {success()}", flush=True)
            for item, quantity, expected in [("missing", 1, grpc.StatusCode.FAILED_PRECONDITION), ("book", 11, grpc.StatusCode.FAILED_PRECONDITION), ("book", 0, grpc.StatusCode.INVALID_ARGUMENT)]:
                try:
                    call(item, quantity)
                    raise AssertionError("Expected gRPC error")
                except grpc.RpcError as exc:
                    assert exc.code() == expected
            print("gRPC unavailable/insufficient: FAILED_PRECONDITION; invalid quantity: INVALID_ARGUMENT", flush=True)
            stop(inventory)
            inventory = start("grpc_service.py", "inventory", ip, "--delay", "1.5")
            # Allow the persistent downstream channel to reconnect after restart.
            deadline = time.monotonic() + 5
            while True:
                started = time.monotonic()
                try:
                    call()
                    raise AssertionError("Expected deadline failure")
                except grpc.RpcError as exc:
                    if exc.code() == grpc.StatusCode.UNAVAILABLE and time.monotonic() < deadline:
                        time.sleep(0.1)
                        continue
                    elapsed = time.monotonic() - started
                    assert exc.code() == grpc.StatusCode.DEADLINE_EXCEEDED
                    assert exc.details() == "inventory_deadline_exceeded" and elapsed < 1.3
                    print(f"gRPC delayed inventory (1.5s), deadline (0.5s): {exc.code().name}: {exc.details()} in {elapsed:.2f}s", flush=True)
                    break
            assert order.poll() is None
            print(f"gRPC after failure: original Order PID {order.pid} alive", flush=True)
            stop(inventory)
            inventory = start("grpc_service.py", "inventory", ip)
            deadline = time.monotonic() + 5
            while True:
                try:
                    result = success()
                    break
                except grpc.RpcError as exc:
                    if exc.code() != grpc.StatusCode.UNAVAILABLE or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
            assert order.poll() is None
            print(f"gRPC recovery: OK {result}; same Order PID {order.pid}", flush=True)
    finally:
        stop(order)
        stop(inventory)


if __name__ == "__main__":
    check_rest()
    check_grpc()
    print("PASS: both transports handle failures and recover without restarting Order.")
