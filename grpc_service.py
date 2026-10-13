import argparse
from concurrent import futures
import time

import grpc
import inventory_pb2 as pb
import inventory_pb2_grpc as rpc
from common import available, validate


def validate_request(request, context):
    try:
        validate(request.item_id, request.quantity)
    except ValueError as exc:
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))


class Inventory(rpc.InventoryServiceServicer):
    def __init__(self, delay):
        self.delay = delay

    def CheckInventory(self, request, context):
        validate_request(request, context)
        end = time.monotonic() + self.delay
        while time.monotonic() < end:
            if not context.is_active():
                context.abort(grpc.StatusCode.CANCELLED, "Caller stopped waiting")
            time.sleep(min(0.02, max(0, end - time.monotonic())))
        if not available(request.item_id, request.quantity):
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, "item_unavailable")
        return pb.InventoryReply(available=True, item_id=request.item_id, quantity=request.quantity)


class Order(rpc.OrderServiceServicer):
    def __init__(self, channel, timeout):
        self.inventory = rpc.InventoryServiceStub(channel)
        self.timeout = timeout

    def PlaceOrder(self, request, context):
        validate_request(request, context)
        remaining = context.time_remaining()
        timeout = min(self.timeout, remaining) if remaining is not None else self.timeout
        try:
            self.inventory.CheckInventory(request, timeout=timeout)
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.DEADLINE_EXCEEDED:
                context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "inventory_deadline_exceeded")
            if exc.code() in (grpc.StatusCode.FAILED_PRECONDITION, grpc.StatusCode.INVALID_ARGUMENT):
                context.abort(exc.code(), exc.details())
            context.abort(grpc.StatusCode.UNAVAILABLE, "inventory_unreachable")
        return pb.OrderReply(status="accepted", item_id=request.item_id, quantity=request.quantity)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=["order", "inventory"])
    parser.add_argument("--port", type=int)
    parser.add_argument("--inventory-target", default="127.0.0.1:50051")
    parser.add_argument("--timeout", type=float, default=0.5)
    parser.add_argument("--delay", type=float, default=0)
    args = parser.parse_args()
    if args.timeout <= 0 or args.delay < 0:
        parser.error("timeout must be positive and delay nonnegative")
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    channel = None
    if args.service == "inventory":
        rpc.add_InventoryServiceServicer_to_server(Inventory(args.delay), server)
    else:
        channel = grpc.insecure_channel(args.inventory_target)
        rpc.add_OrderServiceServicer_to_server(Order(channel, args.timeout), server)
    port = args.port or (50052 if args.service == "order" else 50051)
    server.add_insecure_port(f"127.0.0.1:{port}")
    server.start()
    print(f"gRPC {args.service} listening on {port}", flush=True)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        server.stop(0).wait()
    finally:
        if channel:
            channel.close()
