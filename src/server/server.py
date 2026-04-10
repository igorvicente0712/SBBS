import zmq
import msgpack
import sqlite3
import os
import time

DB_PATH = os.environ.get("DB_PATH", "/data/server.db")
BROKER_ADDR = os.environ.get("BROKER_ADDR", "tcp://broker:5556")
PUBSUB_ADDR = os.environ.get("PUBSUB_ADDR", "tcp://pubsub_proxy:5557")
SERVER_ID = os.environ.get("SERVER_ID", "server")

def get_db():
    conn = sqlite3.connect(DB_PATH)
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
            timestamp INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn

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

def handle_publish(conn, publisher, payload):
    channel = payload.get("channel", "").strip()
    username = payload.get("username", "").strip()
    content = payload.get("content", "").strip()

    if not channel or not username or not content:
        return {"status": "error", "message": "channel, username and content are required"}

    # Verifica se canal existe
    cursor = conn.execute("SELECT name FROM channels WHERE name = ?", (channel,))
    if cursor.fetchone() is None:
        return {"status": "error", "message": f"Channel '{channel}' does not exist"}

    ts = int(time.time())

    # Persiste a mensagem
    conn.execute(
        "INSERT INTO messages (channel, username, content, timestamp) VALUES (?, ?, ?, ?)",
        (channel, username, content, ts)
    )
    conn.commit()

    # Publica no proxy PubSub
    pub_msg = msgpack.packb({
        "username": username,
        "content": content,
        "timestamp": ts
    }, use_bin_type=True)

    topic = channel.encode("utf-8")
    publisher.send_multipart([topic, pub_msg])
    print(f"[{SERVER_ID}] PUB | channel={channel} | username={username} | content={content} | timestamp={ts}", flush=True)

    return {"status": "ok", "message": "Message published", "timestamp": ts}

def handle_get_messages(conn, payload):
    channel = payload.get("channel", "").strip()
    if not channel:
        return {"status": "error", "message": "channel is required"}
    cursor = conn.execute(
        "SELECT username, content, timestamp FROM messages WHERE channel = ? ORDER BY timestamp ASC",
        (channel,)
    )
    msgs = [{"username": r[0], "content": r[1], "timestamp": r[2]} for r in cursor.fetchall()]
    return {"status": "ok", "messages": msgs}

def main():
    conn = get_db()
    context = zmq.Context()

    # Socket REP para receber requisições via broker
    socket = context.socket(zmq.REP)
    socket.connect(BROKER_ADDR)
    print(f"[{SERVER_ID}] Connected to broker at {BROKER_ADDR}", flush=True)

    # Socket PUB para publicar no proxy PubSub
    publisher = context.socket(zmq.PUB)
    publisher.connect(PUBSUB_ADDR)
    print(f"[{SERVER_ID}] Connected to pubsub proxy at {PUBSUB_ADDR}", flush=True)

    # Pequeno delay para o socket PUB estabilizar
    time.sleep(1)

    while True:
        raw = socket.recv()
        msg = msgpack.unpackb(raw, raw=False)
        ts = msg.get("timestamp")
        mtype = msg.get("type")
        payload = msg.get("payload", {})

        print(f"[{SERVER_ID}] RECV | type={mtype} | payload={payload} | timestamp={ts}", flush=True)

        if mtype == "login":
            result = handle_login(conn, payload)
        elif mtype == "create_channel":
            result = handle_create_channel(conn, payload)
        elif mtype == "list_channels":
            result = handle_list_channels(conn)
        elif mtype == "publish":
            result = handle_publish(conn, publisher, payload)
        elif mtype == "get_messages":
            result = handle_get_messages(conn, payload)
        else:
            result = {"status": "error", "message": "Unknown message type"}

        response = {
            "type": "response",
            "payload": result,
            "timestamp": int(time.time())
        }
        socket.send(msgpack.packb(response, use_bin_type=True))
        print(f"[{SERVER_ID}] SEND | payload={result}", flush=True)

if __name__ == "__main__":
    main()