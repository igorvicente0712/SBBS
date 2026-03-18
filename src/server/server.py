import zmq
import msgpack
import sqlite3
import os
import time

DB_PATH = os.environ.get("DB_PATH", "/data/server.db")
BROKER_ADDR = os.environ.get("BROKER_ADDR", "tcp://broker:5556")

def get_db():
    conn = sqlite3.connect(DB_PATH)
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
    conn.commit()
    return conn

def handle_login(conn, payload):
    username = payload.get("username", "").strip()
    if not username:
        return {"status": "error", "message": "Username cannot be empty"}
    ts = int(time.time())
    conn.execute("INSERT OR IGNORE INTO users (username, timestamp) VALUES (?, ?)", (username, ts))
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

def main():
    conn = get_db()
    context = zmq.Context()
    socket = context.socket(zmq.REP)
    socket.connect(BROKER_ADDR)
    print(f"[SERVER] Connected to broker at {BROKER_ADDR}", flush=True)

    while True:
        raw = socket.recv()
        msg = msgpack.unpackb(raw, raw=False)
        ts = msg.get("timestamp")
        mtype = msg.get("type")
        payload = msg.get("payload", {})

        print(f"[SERVER] RECV | type={mtype} | payload={payload} | timestamp={ts}", flush=True)

        if mtype == "login":
            result = handle_login(conn, payload)
        elif mtype == "create_channel":
            result = handle_create_channel(conn, payload)
        elif mtype == "list_channels":
            result = handle_list_channels(conn)
        else:
            result = {"status": "error", "message": "Unknown message type"}

        response = {
            "type": "response",
            "payload": result,
            "timestamp": int(time.time())
        }
        socket.send(msgpack.packb(response, use_bin_type=True))
        print(f"[SERVER] SEND | payload={result}", flush=True)

if __name__ == "__main__":
    main()