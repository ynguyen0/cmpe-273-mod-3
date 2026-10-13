import argparse
import json
import sys

import grpc
import inventory_pb2 as pb
import inventory_pb2_grpc as rpc

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="127.0.0.1:50052")
    parser.add_argument("--item-id", default="book")
    parser.add_argument("--quantity", type=int, default=2)
    args = parser.parse_args()
    with grpc.insecure_channel(args.target) as channel:
        try:
            reply = rpc.OrderServiceStub(channel).PlaceOrder(
                pb.OrderRequest(item_id=args.item_id, quantity=args.quantity), timeout=3,
            )
            print(json.dumps({"status": reply.status, "item_id": reply.item_id, "quantity": reply.quantity}))
        except grpc.RpcError as exc:
            print(f"{exc.code().name}: {exc.details()}")
            sys.exit(1)
