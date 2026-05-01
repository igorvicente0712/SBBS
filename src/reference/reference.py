import zmq
import msgpack
import time
import threading

REFERENCE_PORT = 5560
HEARTBEAT_TIMEOUT = 60

servers = {}
rank_counter = 0
lock = threading.Lock()

def cleanup_loop():
    while True:
        time.sleep(10)
        now = time.time()
        with lock:
            to_remove = [
                name for name, info in servers.items()
                if now - info["last_seen"] > HEARTBEAT_TIMEOUT
            ]
            for name in to_remove:
                print(f"[REFERENCE] Server '{name}' removed (heartbeat timeout)", flush=True)
                del servers[name]

def main():
    global rank_counter

    threading.Thread(target=cleanup_loop, daemon=True).start()

    context = zmq.Context()
    socket = context.socket(zmq.REP)
    socket.bind(f"tcp://*:{REFERENCE_PORT}")
    print(f"[REFERENCE] Running on port {REFERENCE_PORT}", flush=True)

    while True:
        raw = socket.recv()
        msg = msgpack.unpackb(raw, raw=False)

        mtype = msg.get("type")
        payload = msg.get("payload", {})
        logical_clock = msg.get("logical_clock", 0)

        print(f"[REFERENCE] RECV | type={mtype} | payload={payload} | logical_clock={logical_clock}", flush=True)

        now = time.time()

        if mtype == "register":
            name = payload.get("name", "").strip()
            with lock:
                if name and name not in servers:
                    rank_counter += 1
                    servers[name] = {"rank": rank_counter, "last_seen": now}
                    rank = rank_counter
                    print(f"[REFERENCE] Registered server '{name}' with rank {rank}", flush=True)
                elif name in servers:
                    rank = servers[name]["rank"]
                    servers[name]["last_seen"] = now
                else:
                    rank = -1

            result = {"status": "ok", "rank": rank}

        elif mtype == "list":
            with lock:
                server_list = [
                    {"name": n, "rank": info["rank"]}
                    for n, info in sorted(servers.items(), key=lambda x: x[1]["rank"])
                ]
            result = {"status": "ok", "servers": server_list}

        elif mtype == "heartbeat":
            name = payload.get("name", "").strip()
            with lock:
                if name in servers:
                    servers[name]["last_seen"] = now
                    print(f"[REFERENCE] Heartbeat from '{name}'", flush=True)
                else:
                    print(f"[REFERENCE] Heartbeat from unknown server '{name}', ignoring", flush=True)

            # Timestamp removido conforme especificação da parte 4
            result = {"status": "ok"}

        else:
            result = {"status": "error", "message": "Unknown message type"}

        response = msgpack.packb({"type": "response", "payload": result}, use_bin_type=True)
        socket.send(response)
        print(f"[REFERENCE] SEND | payload={result}", flush=True)

if __name__ == "__main__":
    main()