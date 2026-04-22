import zmq

def main():
    context = zmq.Context()

    xsub = context.socket(zmq.XSUB)
    xsub.bind("tcp://*:5557")

    xpub = context.socket(zmq.XPUB)
    xpub.bind("tcp://*:5558")

    print("[PUBSUB PROXY] Running on ports 5557 (XSUB) and 5558 (XPUB)", flush=True)

    zmq.proxy(xsub, xpub)

if __name__ == "__main__":
    main()