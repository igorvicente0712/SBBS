import zmq
import msgpack
import sqlite3
import os
import time
import threading

DB_PATH = os.environ.get("DB_PATH", "/data/server.db")
BROKER_ADDR = os.environ.get("BROKER_ADDR", "tcp://broker:5556")
PUBSUB_ADDR = os.environ.get("PUBSUB_ADDR", "tcp://pubsub_proxy:5557")
PUBSUB_SUB_ADDR = os.environ.get("PUBSUB_SUB_ADDR", "tcp://pubsub_proxy:5558")
REFERENCE_ADDR = os.environ.get("REFERENCE_ADDR", "tcp://reference:5560")
SERVER_ID = os.environ.get("SERVER_ID", "server")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "5570"))
PEERS_RAW = os.environ.get("PEERS", "")

logical_clock = 0
clock_lock = threading.Lock()

message_counter = 0
message_counter_lock = threading.Lock()

coordinator = None
server_rank = -1
coordinator_lock = threading.Lock()

zmq_context = None


def increment_clock():
    global logical_clock
    with clock_lock:
        logical_clock += 1
        return logical_clock


def update_clock(received):
    global logical_clock
    with clock_lock:
        logical_clock = max(logical_clock, received) + 1
        return logical_clock


def parse_peers():
    peers = {}
    if not PEERS_RAW:
        return peers
    for entry in PEERS_RAW.split(","):
        parts = entry.strip().split(":")
        if len(parts) == 3:
            peer_id, host, port = parts
            peers[peer_id.strip()] = {"host": host.strip(), "port": int(port.strip())}
    return peers


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.commit()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            timestamp INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            name TEXT PRIMARY KEY,
            created_by TEXT,
            timestamp INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            username TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            logical_clock INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def send_to_reference(mtype, payload):
    sock = zmq_context.socket(zmq.REQ)
    sock.connect(REFERENCE_ADDR)
    sock.setsockopt(zmq.RCVTIMEO, 5000)
    sock.setsockopt(zmq.LINGER, 0)
    try:
        clk = increment_clock()
        msg = msgpack.packb(
            {"type": mtype, "payload": payload, "logical_clock": clk},
            use_bin_type=True
        )
        print(f"[{SERVER_ID}] SEND → reference | type={mtype} | payload={payload} | logical_clock={clk}", flush=True)
        sock.send(msg)
        raw = sock.recv()
        resp = msgpack.unpackb(raw, raw=False)
        recv_clk = resp.get("logical_clock", 0)
        update_clock(recv_clk)
        print(f"[{SERVER_ID}] RECV ← reference | payload={resp.get('payload')} | logical_clock={recv_clk}", flush=True)
        return resp.get("payload", {})
    except zmq.Again:
        print(f"[{SERVER_ID}] Timeout communicating with reference", flush=True)
        return {}
    finally:
        sock.close()


def register_with_reference():
    while True:
        result = send_to_reference("register", {"name": SERVER_ID})
        if result.get("status") == "ok":
            rank = result.get("rank", -1)
            print(f"[{SERVER_ID}] Registered with reference | rank={rank}", flush=True)
            return rank
        print(f"[{SERVER_ID}] Failed to register with reference, retrying...", flush=True)
        time.sleep(2)


def send_heartbeat():
    result = send_to_reference("heartbeat", {"name": SERVER_ID})
    if result.get("status") == "ok":
        print(f"[{SERVER_ID}] Heartbeat OK", flush=True)


# ---------------------------------------------------------------------------
# Comunicação direta entre servidores
# ---------------------------------------------------------------------------

def send_to_peer(peer_id, host, port, mtype, payload, timeout=3000):
    sock = zmq_context.socket(zmq.REQ)
    addr = f"tcp://{host}:{port}"
    sock.connect(addr)
    sock.setsockopt(zmq.RCVTIMEO, timeout)
    sock.setsockopt(zmq.LINGER, 0)
    try:
        clk = increment_clock()
        msg = msgpack.packb(
            {"type": mtype, "payload": payload, "logical_clock": clk},
            use_bin_type=True
        )
        print(f"[{SERVER_ID}] SEND → {peer_id} | type={mtype} | payload={payload} | logical_clock={clk}", flush=True)
        sock.send(msg)
        raw = sock.recv()
        resp = msgpack.unpackb(raw, raw=False)
        recv_clk = resp.get("logical_clock", 0)
        update_clock(recv_clk)
        print(f"[{SERVER_ID}] RECV ← {peer_id} | payload={resp.get('payload')} | logical_clock={recv_clk}", flush=True)
        return resp.get("payload", {})
    except zmq.Again:
        print(f"[{SERVER_ID}] Timeout communicating with peer {peer_id}", flush=True)
        return None
    finally:
        sock.close()


def replicate_to_peers(peers, operation, data):
    """
    Envia uma operação de escrita para todos os peers replicarem.
    Executado em thread separada para não bloquear a resposta ao cliente.
    operation: "login" | "create_channel" | "publish"
    data: dicionário com os dados necessários para replicar a operação
    """
    for peer_id, info in peers.items():
        payload = {"operation": operation, "data": data}
        result = send_to_peer(peer_id, info["host"], info["port"], "replicate", payload)
        if result is None:
            print(f"[{SERVER_ID}] Replication to {peer_id} failed (peer may be down)", flush=True)
        elif result.get("status") == "ok":
            print(f"[{SERVER_ID}] Replication to {peer_id} OK | operation={operation}", flush=True)
        else:
            print(f"[{SERVER_ID}] Replication to {peer_id} returned error | operation={operation} | result={result}", flush=True)


def apply_replicated_operation(conn, operation, data):
    """
    Aplica localmente uma operação replicada recebida de um peer.
    Não re-replica para evitar loop infinito.
    """
    try:
        if operation == "login":
            username = data.get("username", "").strip()
            ts = data.get("timestamp", int(time.time()))
            if username:
                conn.execute(
                    "INSERT OR IGNORE INTO users (username, timestamp) VALUES (?, ?)",
                    (username, ts)
                )
                conn.commit()
                print(f"[{SERVER_ID}] REPLICATED login | username={username}", flush=True)

        elif operation == "create_channel":
            name = data.get("name", "").strip()
            created_by = data.get("created_by", "").strip()
            ts = data.get("timestamp", int(time.time()))
            if name:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO channels (name, created_by, timestamp) VALUES (?, ?, ?)",
                        (name, created_by, ts)
                    )
                    conn.commit()
                    print(f"[{SERVER_ID}] REPLICATED create_channel | name={name} | created_by={created_by}", flush=True)
                except sqlite3.IntegrityError:
                    print(f"[{SERVER_ID}] REPLICATED create_channel | channel '{name}' already exists, skipping", flush=True)

        elif operation == "publish":
            channel = data.get("channel", "").strip()
            username = data.get("username", "").strip()
            content = data.get("content", "").strip()
            ts = data.get("timestamp", int(time.time()))
            clk = data.get("logical_clock", 0)
            if channel and username and content:
                # Garante que o canal existe antes de inserir a mensagem
                conn.execute(
                    "INSERT OR IGNORE INTO channels (name, created_by, timestamp) VALUES (?, ?, ?)",
                    (channel, username, ts)
                )
                conn.execute(
                    "INSERT INTO messages (channel, username, content, timestamp, logical_clock) VALUES (?, ?, ?, ?, ?)",
                    (channel, username, content, ts, clk)
                )
                conn.commit()
                print(
                    f"[{SERVER_ID}] REPLICATED publish | channel={channel} | username={username} | "
                    f"content={content} | logical_clock={clk}",
                    flush=True
                )

        else:
            print(f"[{SERVER_ID}] Unknown replicated operation: {operation}", flush=True)

    except Exception as e:
        print(f"[{SERVER_ID}] Error applying replicated operation '{operation}': {e}", flush=True)


def peer_server_loop(peers):
    # Cada thread de peer_server_loop usa sua própria conexão com o banco
    conn = get_db()

    sock = zmq_context.socket(zmq.REP)
    sock.bind(f"tcp://*:{SERVER_PORT}")
    print(f"[{SERVER_ID}] Peer listener on port {SERVER_PORT}", flush=True)

    while True:
        try:
            raw = sock.recv()
            msg = msgpack.unpackb(raw, raw=False)
            mtype = msg.get("type")
            payload = msg.get("payload", {})
            recv_clk = msg.get("logical_clock", 0)
            clk = update_clock(recv_clk)

            print(f"[{SERVER_ID}] PEER RECV | type={mtype} | payload={payload} | logical_clock={clk}", flush=True)

            if mtype == "election":
                result = {"status": "ok"}

            elif mtype == "get_clock":
                result = {"status": "ok", "timestamp": int(time.time())}

            elif mtype == "coordinator":
                new_coord = payload.get("coordinator")
                with coordinator_lock:
                    global coordinator
                    coordinator = new_coord
                print(f"[{SERVER_ID}] New coordinator announced via peer msg: {new_coord}", flush=True)
                result = {"status": "ok"}

            elif mtype == "replicate":
                # Aplica a operação replicada localmente sem re-replicar
                operation = payload.get("operation")
                data = payload.get("data", {})
                apply_replicated_operation(conn, operation, data)
                result = {"status": "ok"}

            else:
                result = {"status": "error", "message": "Unknown peer message type"}

            send_clk = increment_clock()
            response = msgpack.packb(
                {"type": "response", "payload": result, "logical_clock": send_clk},
                use_bin_type=True
            )
            sock.send(response)
            print(f"[{SERVER_ID}] PEER SEND | payload={result} | logical_clock={send_clk}", flush=True)

        except Exception as e:
            print(f"[{SERVER_ID}] Error in peer_server_loop: {e}", flush=True)


def announce_coordinator(publisher):
    clk = increment_clock()
    pub_msg = msgpack.packb(
        {"coordinator": SERVER_ID, "logical_clock": clk},
        use_bin_type=True
    )
    topic = b"servers"
    publisher.send_multipart([topic, pub_msg])
    print(f"[{SERVER_ID}] PUB | topic=servers | coordinator={SERVER_ID} | logical_clock={clk}", flush=True)


def start_election(peers, publisher):
    global coordinator, server_rank

    print(f"[{SERVER_ID}] Starting election | my rank={server_rank}", flush=True)

    higher_responded = False

    for peer_id, info in peers.items():
        peer_rank = info.get("rank", -1)
        if peer_rank > server_rank:
            result = send_to_peer(peer_id, info["host"], info["port"], "election", {"from": SERVER_ID})
            if result is not None and result.get("status") == "ok":
                higher_responded = True
                print(f"[{SERVER_ID}] Peer {peer_id} responded to election — waiting for coordinator announcement", flush=True)

    if not higher_responded:
        with coordinator_lock:
            coordinator = SERVER_ID
        print(f"[{SERVER_ID}] Elected as coordinator!", flush=True)
        announce_coordinator(publisher)


def sync_clock_with_coordinator(peers):
    global coordinator

    with coordinator_lock:
        current_coord = coordinator

    if current_coord is None or current_coord == SERVER_ID:
        return

    if current_coord not in peers:
        print(f"[{SERVER_ID}] Coordinator {current_coord} not in peers list", flush=True)
        return

    info = peers[current_coord]
    result = send_to_peer(current_coord, info["host"], info["port"], "get_clock", {"from": SERVER_ID})

    if result is None:
        print(f"[{SERVER_ID}] Coordinator {current_coord} unreachable — starting election", flush=True)
        with coordinator_lock:
            coordinator = None
        return

    coord_ts = result.get("timestamp")
    if coord_ts is not None:
        local_ts = int(time.time())
        drift = coord_ts - local_ts
        new_clk = increment_clock()
        print(
            f"[{SERVER_ID}] Clock sync with coordinator={current_coord} | "
            f"coord_ts={coord_ts} | local_ts={local_ts} | drift={drift}s | new_logical_clock={new_clk}",
            flush=True
        )


def subscriber_loop(peers, publisher):
    global coordinator

    sock = zmq_context.socket(zmq.SUB)
    sock.connect(PUBSUB_SUB_ADDR)
    sock.setsockopt(zmq.SUBSCRIBE, b"servers")
    sock.setsockopt(zmq.RCVTIMEO, 5000)

    print(f"[{SERVER_ID}] Subscribed to topic 'servers' on {PUBSUB_SUB_ADDR}", flush=True)

    while True:
        try:
            parts = sock.recv_multipart()
            if len(parts) < 2:
                continue
            topic = parts[0].decode("utf-8")
            data = msgpack.unpackb(parts[1], raw=False)

            recv_clk = data.get("logical_clock", 0)
            update_clock(recv_clk)

            new_coord = data.get("coordinator")
            if new_coord:
                with coordinator_lock:
                    coordinator = new_coord
                print(f"[{SERVER_ID}] SUB RECV | topic={topic} | new coordinator={new_coord}", flush=True)

        except zmq.Again:
            with coordinator_lock:
                current_coord = coordinator

            if current_coord is not None and current_coord != SERVER_ID and current_coord in peers:
                info = peers[current_coord]
                result = send_to_peer(current_coord, info["host"], info["port"], "get_clock", {"from": SERVER_ID}, timeout=2000)
                if result is None:
                    print(f"[{SERVER_ID}] Coordinator {current_coord} seems down — starting election", flush=True)
                    with coordinator_lock:
                        coordinator = None
                    start_election(peers, publisher)

        except Exception as e:
            print(f"[{SERVER_ID}] Error in subscriber_loop: {e}", flush=True)


# ---------------------------------------------------------------------------
# Handlers de mensagens dos clientes
# ---------------------------------------------------------------------------

def handle_login(conn, payload):
    username = payload.get("username", "").strip()
    if not username:
        return {"status": "error", "message": "Username cannot be empty"}, None
    ts = int(time.time())
    conn.execute(
        "INSERT OR IGNORE INTO users (username, timestamp) VALUES (?, ?)",
        (username, ts)
    )
    conn.commit()
    replication_data = {"username": username, "timestamp": ts}
    return {"status": "ok", "message": f"Welcome, {username}"}, replication_data


def handle_create_channel(conn, payload):
    name = payload.get("name", "").strip()
    username = payload.get("username", "").strip()
    if not name:
        return {"status": "error", "message": "Channel name cannot be empty"}, None
    if not name.isalnum():
        return {"status": "error", "message": "Channel name must be alphanumeric"}, None
    ts = int(time.time())
    try:
        conn.execute(
            "INSERT INTO channels (name, created_by, timestamp) VALUES (?, ?, ?)",
            (name, username, ts)
        )
        conn.commit()
        replication_data = {"name": name, "created_by": username, "timestamp": ts}
        return {"status": "ok", "message": f"Channel '{name}' created"}, replication_data
    except sqlite3.IntegrityError:
        return {"status": "error", "message": f"Channel '{name}' already exists"}, None


def handle_list_channels(conn):
    cursor = conn.execute("SELECT name FROM channels")
    channels = [row[0] for row in cursor.fetchall()]
    return {"status": "ok", "channels": channels}, None


def handle_publish(conn, publisher, payload, clk):
    channel = payload.get("channel", "").strip()
    username = payload.get("username", "").strip()
    content = payload.get("content", "").strip()

    if not channel or not username or not content:
        return {"status": "error", "message": "channel, username and content are required"}, None

    cursor = conn.execute("SELECT name FROM channels WHERE name = ?", (channel,))
    if cursor.fetchone() is None:
        return {"status": "error", "message": f"Channel '{channel}' does not exist"}, None

    ts = int(time.time())
    conn.execute(
        "INSERT INTO messages (channel, username, content, timestamp, logical_clock) VALUES (?, ?, ?, ?, ?)",
        (channel, username, content, ts, clk)
    )
    conn.commit()

    pub_msg = msgpack.packb({
        "username": username,
        "content": content,
        "timestamp": ts,
        "logical_clock": clk
    }, use_bin_type=True)

    topic = channel.encode("utf-8")
    publisher.send_multipart([topic, pub_msg])
    print(f"[{SERVER_ID}] PUB | channel={channel} | username={username} | content={content} | timestamp={ts} | logical_clock={clk}", flush=True)

    replication_data = {
        "channel": channel,
        "username": username,
        "content": content,
        "timestamp": ts,
        "logical_clock": clk
    }
    return {"status": "ok", "message": "Message published", "timestamp": ts}, replication_data


def handle_get_messages(conn, payload):
    channel = payload.get("channel", "").strip()
    if not channel:
        return {"status": "error", "message": "channel is required"}, None
    cursor = conn.execute(
        "SELECT username, content, timestamp, logical_clock FROM messages WHERE channel = ? ORDER BY logical_clock ASC, timestamp ASC",
        (channel,)
    )
    msgs = [
        {"username": r[0], "content": r[1], "timestamp": r[2], "logical_clock": r[3]}
        for r in cursor.fetchall()
    ]
    return {"status": "ok", "messages": msgs}, None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global zmq_context, server_rank, coordinator, message_counter

    zmq_context = zmq.Context()
    conn = get_db()

    peers_raw = parse_peers()

    time.sleep(2)
    server_rank = register_with_reference()

    ref_list = send_to_reference("list", {})
    if ref_list.get("status") == "ok":
        for entry in ref_list.get("servers", []):
            pid = entry["name"]
            if pid in peers_raw:
                peers_raw[pid]["rank"] = entry["rank"]

    socket = zmq_context.socket(zmq.REP)
    socket.connect(BROKER_ADDR)
    print(f"[{SERVER_ID}] Connected to broker at {BROKER_ADDR} | rank={server_rank}", flush=True)

    publisher = zmq_context.socket(zmq.PUB)
    publisher.connect(PUBSUB_ADDR)
    print(f"[{SERVER_ID}] Connected to pubsub proxy at {PUBSUB_ADDR}", flush=True)

    time.sleep(1)

    threading.Thread(target=peer_server_loop, args=(peers_raw,), daemon=True).start()
    threading.Thread(target=subscriber_loop, args=(peers_raw, publisher), daemon=True).start()

    time.sleep(3)
    start_election(peers_raw, publisher)

    while True:
        raw = socket.recv()
        msg = msgpack.unpackb(raw, raw=False)

        recv_logical = msg.get("logical_clock", 0)
        clk = update_clock(recv_logical)

        ts = msg.get("timestamp")
        mtype = msg.get("type")
        payload = msg.get("payload", {})

        print(f"[{SERVER_ID}] RECV | type={mtype} | payload={payload} | timestamp={ts} | logical_clock={clk}", flush=True)

        replication_data = None

        if mtype == "login":
            result, replication_data = handle_login(conn, payload)
        elif mtype == "create_channel":
            result, replication_data = handle_create_channel(conn, payload)
        elif mtype == "list_channels":
            result, replication_data = handle_list_channels(conn)
        elif mtype == "publish":
            result, replication_data = handle_publish(conn, publisher, payload, clk)
        elif mtype == "get_messages":
            result, replication_data = handle_get_messages(conn, payload)
        else:
            result = {"status": "error", "message": "Unknown message type"}
            replication_data = None

        # Dispara replicação em background para não bloquear a resposta ao cliente
        if replication_data is not None and peers_raw:
            threading.Thread(
                target=replicate_to_peers,
                args=(peers_raw, mtype, replication_data),
                daemon=True
            ).start()

        with message_counter_lock:
            message_counter += 1
            should_sync = (message_counter % 15 == 0)
            should_heartbeat = (message_counter % 10 == 0)

        if should_heartbeat:
            threading.Thread(target=send_heartbeat, daemon=True).start()

        if should_sync:
            with coordinator_lock:
                current_coord = coordinator
            if current_coord is None:
                threading.Thread(target=start_election, args=(peers_raw, publisher), daemon=True).start()
            elif current_coord != SERVER_ID:
                threading.Thread(target=sync_clock_with_coordinator, args=(peers_raw,), daemon=True).start()

        send_clk = increment_clock()
        response = {
            "type": "response",
            "payload": result,
            "timestamp": int(time.time()),
            "logical_clock": send_clk
        }
        socket.send(msgpack.packb(response, use_bin_type=True))
        print(f"[{SERVER_ID}] SEND | payload={result} | logical_clock={send_clk}", flush=True)


if __name__ == "__main__":
    main()