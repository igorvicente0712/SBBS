import zmq
import msgpack
import sqlite3
import os
import time
import threading

DB_PATH = os.environ.get("DB_PATH", "/data/server.db")
BROKER_ADDR = os.environ.get("BROKER_ADDR", "tcp://broker:5556")
PUBSUB_ADDR = os.environ.get("PUBSUB_ADDR", "tcp://pubsub_proxy:5557")
REFERENCE_ADDR = os.environ.get("REFERENCE_ADDR", "tcp://reference:5560")
SERVER_ID = os.environ.get("SERVER_ID", "server")

# Relógio lógico
logical_clock = 0
clock_lock = threading.Lock()

# Contador de mensagens para heartbeat
message_counter = 0
message_counter_lock = threading.Lock()


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


def send_to_reference(context, mtype, payload):
    """Envia uma mensagem ao serviço de referência e retorna o payload da resposta."""
    sock = context.socket(zmq.REQ)
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


def register_with_reference(context):
    """Registra o servidor no serviço de referência e obtém seu rank."""
    while True:
        result = send_to_reference(context, "register", {"name": SERVER_ID})
        if result.get("status") == "ok":
            rank = result.get("rank", -1)
            print(f"[{SERVER_ID}] Registered with reference | rank={rank}", flush=True)
            return rank
        print(f"[{SERVER_ID}] Failed to register with reference, retrying...", flush=True)
        time.sleep(2)


def send_heartbeat(context):
    """Envia heartbeat ao reference e sincroniza o relógio físico."""
    result = send_to_reference(context, "heartbeat", {"name": SERVER_ID})
    if result.get("status") == "ok":
        ref_ts = result.get("timestamp")
        if ref_ts:
            local_ts = int(time.time())
            drift = ref_ts - local_ts
            print(f"[{SERVER_ID}] Heartbeat OK | ref_timestamp={ref_ts} | local_timestamp={local_ts} | drift={drift}s", flush=True)


def handle_login(conn, payload):
    username = payload.get("username", "").strip()
    if not username:
        return {"status": "error", "message": "Username cannot be empty"}
    ts = int(time.time())
    conn.execute(
        "INSERT OR IGNORE INTO users (username, timestamp) VALUES (?, ?)",
        (username, ts)
    )
    conn.commit()
    return {"status": "ok", "message": f"Welcome, {username}"}


def handle_create_channel(conn, payload):
    name = payload.get("name", "").strip()
    username = payload.get("username", "").strip()
    if not name:
        return {"status": "error", "message": "Channel name cannot be empty"}
    if not name.isalnum():
        return {"status": "error", "message": "Channel name must be alphanumeric"}
    ts = int(time.time())
    try:
        conn.execute(
            "INSERT INTO channels (name, created_by, timestamp) VALUES (?, ?, ?)",
            (name, username, ts)
        )
        conn.commit()
        return {"status": "ok", "message": f"Channel '{name}' created"}
    except sqlite3.IntegrityError:
        return {"status": "error", "message": f"Channel '{name}' already exists"}


def handle_list_channels(conn):
    cursor = conn.execute("SELECT name FROM channels")
    channels = [row[0] for row in cursor.fetchall()]
    return {"status": "ok", "channels": channels}


def handle_publish(conn, publisher, payload, clk):
    channel = payload.get("channel", "").strip()
    username = payload.get("username", "").strip()
    content = payload.get("content", "").strip()

    if not channel or not username or not content:
        return {"status": "error", "message": "channel, username and content are required"}

    cursor = conn.execute("SELECT name FROM channels WHERE name = ?", (channel,))
    if cursor.fetchone() is None:
        return {"status": "error", "message": f"Channel '{channel}' does not exist"}

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

    return {"status": "ok", "message": "Message published", "timestamp": ts}


def handle_get_messages(conn, payload):
    channel = payload.get("channel", "").strip()
    if not channel:
        return {"status": "error", "message": "channel is required"}
    cursor = conn.execute(
        "SELECT username, content, timestamp, logical_clock FROM messages WHERE channel = ? ORDER BY logical_clock ASC, timestamp ASC",
        (channel,)
    )
    msgs = [
        {"username": r[0], "content": r[1], "timestamp": r[2], "logical_clock": r[3]}
        for r in cursor.fetchall()
    ]
    return {"status": "ok", "messages": msgs}


def main():
    global message_counter

    conn = get_db()
    context = zmq.Context()

    # Registra no reference
    time.sleep(2)
    server_rank = register_with_reference(context)

    # Socket REP para receber requisições via broker
    socket = context.socket(zmq.REP)
    socket.connect(BROKER_ADDR)
    print(f"[{SERVER_ID}] Connected to broker at {BROKER_ADDR} | rank={server_rank}", flush=True)

    # Socket PUB para publicar no proxy PubSub
    publisher = context.socket(zmq.PUB)
    publisher.connect(PUBSUB_ADDR)
    print(f"[{SERVER_ID}] Connected to pubsub proxy at {PUBSUB_ADDR}", flush=True)

    time.sleep(1)

    while True:
        raw = socket.recv()
        msg = msgpack.unpackb(raw, raw=False)

        recv_logical = msg.get("logical_clock", 0)
        clk = update_clock(recv_logical)

        ts = msg.get("timestamp")
        mtype = msg.get("type")
        payload = msg.get("payload", {})

        print(f"[{SERVER_ID}] RECV | type={mtype} | payload={payload} | timestamp={ts} | logical_clock={clk}", flush=True)

        if mtype == "login":
            result = handle_login(conn, payload)
        elif mtype == "create_channel":
            result = handle_create_channel(conn, payload)
        elif mtype == "list_channels":
            result = handle_list_channels(conn)
        elif mtype == "publish":
            result = handle_publish(conn, publisher, payload, clk)
        elif mtype == "get_messages":
            result = handle_get_messages(conn, payload)
        else:
            result = {"status": "error", "message": "Unknown message type"}

        # Incrementa contador de mensagens e envia heartbeat a cada 10
        with message_counter_lock:
            message_counter += 1
            should_heartbeat = (message_counter % 10 == 0)

        if should_heartbeat:
            send_heartbeat(context)

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