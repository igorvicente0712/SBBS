import zmq

def main():
    context = zmq.Context()

    frontend = context.socket(zmq.ROUTER)
    frontend.bind("tcp://*:5555")

    backend = context.socket(zmq.DEALER)
    backend.bind("tcp://*:5556")

    print("[BROKER] Running on ports 5555 (frontend) and 5556 (backend)", flush=True)

    zmq.proxy(frontend, backend)

if __name__ == "__main__":
    main()