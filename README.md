# Order to Inventory: REST and gRPC

Two equivalent Python implementations of an Order Service calling an Inventory
Service. Each accepts `item_id` and `quantity`, validates them, and accepts the
order only when stock is sufficient. Inventory contains `book: 10` and `pen: 100`.
This is a read-only availability demonstration: orders are not persisted and
stock is not reserved or decremented. Unknown items and insufficient quantities
both count as unavailable.

## Setup

Use Python 3.10 or newer (verified with Python 3.14.7). Run from this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Generated protobuf modules are included. Regenerate them after changing the contract:

```sh
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. inventory.proto
```

Activate the virtual environment in each terminal used below. All services bind
to localhost; these examples use plaintext local connections.

## Run REST

Terminal 1 — Inventory Service:

```sh
python rest_service.py inventory --port 8001
```

Terminal 2 — Order Service:

```sh
python rest_service.py order --port 8000 --inventory-url http://127.0.0.1:8001 --timeout 0.5
```

Terminal 3 — submit an order:

```sh
curl -i -X POST http://127.0.0.1:8000/orders \
  -H 'Content-Type: application/json' \
  -d '{"item_id":"book","quantity":2}'
```

Successful response (nonessential headers omitted):

```http
HTTP/1.0 201 Created
Content-Type: application/json

{"status": "accepted", "item_id": "book", "quantity": 2}
```

Order calls `POST /inventory/check` with the same JSON and an explicit 0.5-second
socket timeout. You can also call inventory directly:

```sh
curl -i http://127.0.0.1:8001/inventory/check \
  -H 'Content-Type: application/json' \
  -d '{"item_id":"book","quantity":2}'
```

```http
HTTP/1.0 200 OK
Content-Type: application/json

{"available": true, "item_id": "book", "quantity": 2}
```

REST returns `400` for invalid input, `409` for unavailable stock, `415` for a
non-JSON content type, and `404` for unknown routes. Order maps inventory timeouts
to `504`, connection failures to `503`, and unexpected upstream errors to `502`.
The socket timeout bounds blocked network operations; it is not a total elapsed
deadline across all phases of an HTTP request.

## Run gRPC

`inventory.proto` defines `InventoryService.CheckInventory` and
`OrderService.PlaceOrder`. Both take an `OrderRequest` containing a string
`item_id` and an int32 `quantity`. The inventory reply contains `available`,
`item_id`, and `quantity`; the order reply contains `status`, `item_id`, and
`quantity`.

Terminal 1 — Inventory Service:

```sh
python grpc_service.py inventory --port 50051
```

Terminal 2 — Order Service:

```sh
python grpc_service.py order --port 50052 --inventory-target 127.0.0.1:50051 --timeout 0.5
```

Terminal 3 — submit an order using the generated stub:

```sh
python grpc_client.py --item-id book --quantity 2
```

Successful response:

```text
{"status": "accepted", "item_id": "book", "quantity": 2}
```

The client prints the protobuf response as JSON for readability; the wire format
is protobuf. Its outer deadline is 3 seconds so the Order Service can report its
shorter downstream deadline. Order passes `timeout=0.5` to the inventory stub
(or the remaining inbound deadline if shorter).

Unavailable stock produces an actual gRPC error, propagated by Order:

```sh
python grpc_client.py --item-id book --quantity 11
```

```text
FAILED_PRECONDITION: item_unavailable
```

Invalid input produces `INVALID_ARGUMENT`; connection failure produces
`UNAVAILABLE`. The example client exits with status 1 on RPC errors.

## Reproduce slow-inventory failures manually

Keep each **Order Service running** throughout its test. In the corresponding
Inventory terminal, press Ctrl-C and restart inventory with a 1.5-second delay:

```sh
# REST inventory terminal
python rest_service.py inventory --port 8001 --delay 1.5

# gRPC inventory terminal
python grpc_service.py inventory --port 50051 --delay 1.5
```

Run the same successful order commands above again. REST now returns:

```http
HTTP/1.0 504 Gateway Timeout
Content-Type: application/json

{"error": "inventory_timeout"}
```

gRPC now prints:

```text
DEADLINE_EXCEEDED: inventory_deadline_exceeded
```

Verify REST Order is still serving requests:

```sh
curl -i http://127.0.0.1:8000/health
```

```http
HTTP/1.0 200 OK
Content-Type: application/json

{"status": "ok", "service": "order"}
```

Stop and restart each inventory without `--delay`, leaving both Order processes
untouched. Repeat the order commands: both succeed again. The persistent gRPC
channel may briefly return `UNAVAILABLE` while reconnecting after an inventory
restart; retry after a moment. The REST inventory tolerates clients disconnecting
after timeout, and the gRPC inventory stops delayed work when its context is
cancelled.

## Automated checks and captured evidence

```sh
python verify.py
```

This runs real service processes on temporary localhost ports, checks successful
orders, unknown items, insufficient stock, invalid quantities, and both timeout
paths. It checks that the original Order process is alive and successfully handles
another order after inventory recovers, then cleans up all subprocesses. It requires
no manually running services. Timing assertions expect each 0.5-second timeout to
finish within 1.3 seconds, before the configured 1.5-second delay.

Captured output from a passing run (a gRPC library diagnostic line omitted):

```text
REST success: HTTP 201 {"status": "accepted", "item_id": "book", "quantity": 2}
REST unavailable/insufficient: HTTP 409; invalid quantity: HTTP 400
REST delayed inventory (1.5s), timeout (0.5s): HTTP 504 {"error": "inventory_timeout"} in 0.50s
REST after failure: HTTP 200 {"status": "ok", "service": "order"}; original Order PID 70995 alive
REST recovery: HTTP 201 {"status": "accepted", "item_id": "book", "quantity": 2}; same Order PID 70995
gRPC success: OK {"status": "accepted", "item_id": "book", "quantity": 2}
gRPC unavailable/insufficient: FAILED_PRECONDITION; invalid quantity: INVALID_ARGUMENT
gRPC delayed inventory (1.5s), deadline (0.5s): DEADLINE_EXCEEDED: inventory_deadline_exceeded in 0.51s
gRPC after failure: original Order PID 71001 alive
gRPC recovery: OK {"status": "accepted", "item_id": "book", "quantity": 2}; same Order PID 71001
PASS: both transports handle failures and recover without restarting Order.
```

PIDs and elapsed times vary between runs. Matching PIDs before and after recovery
show that Order was not restarted.

## Comparison (5 sentences)

REST was straightforward to inspect with curl because requests and responses were
plain JSON, but it required manual routing, JSON parsing, and field validation.
gRPC required a `.proto` contract and a code-generation step, after which generated
messages and stubs made service calls explicit and typed.
Both implementations still needed application validation for positive quantities
and sufficient stock.
REST required mapping timeout exceptions to HTTP 504, while gRPC exposed
`DEADLINE_EXCEEDED` and a cancellation-aware server context directly.
In the delay test both failed in about half a second and recovered without
restarting Order, although gRPC needed a generated client rather than ordinary curl.
