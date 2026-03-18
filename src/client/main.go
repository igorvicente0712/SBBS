package main

import (
    "fmt"
    "math/rand"
    "os"
    "time"

    zmq "github.com/pebbe/zmq4"
    "github.com/vmihailenco/msgpack/v5"
)

type Message struct {
    Type      string                 `msgpack:"type"`
    Payload   map[string]interface{} `msgpack:"payload"`
    Timestamp int64                  `msgpack:"timestamp"`
}

type Response struct {
    Type      string                 `msgpack:"type"`
    Payload   map[string]interface{} `msgpack:"payload"`
    Timestamp int64                  `msgpack:"timestamp"`
}

var brokerAddr = os.Getenv("BROKER_ADDR")
var botName = os.Getenv("BOT_NAME")

func send(socket *zmq.Socket, msgType string, payload map[string]interface{}) (Response, error) {
    msg := Message{
        Type:      msgType,
        Payload:   payload,
        Timestamp: time.Now().Unix(),
    }
    raw, err := msgpack.Marshal(msg)
    if err != nil {
        return Response{}, err
    }
    fmt.Printf("[CLIENT:%s] SEND | type=%s | payload=%v | timestamp=%d\n", botName, msgType, payload, msg.Timestamp)
    _, err = socket.SendBytes(raw, 0)
    if err != nil {
        return Response{}, err
    }
    respRaw, err := socket.RecvBytes(0)
    if err != nil {
        return Response{}, err
    }
    var resp Response
    err = msgpack.Unmarshal(respRaw, &resp)
    if err != nil {
        return Response{}, err
    }
    fmt.Printf("[CLIENT:%s] RECV | payload=%v | timestamp=%d\n", botName, resp.Payload, resp.Timestamp)
    return resp, nil
}

func main() {
    if brokerAddr == "" {
        brokerAddr = "tcp://broker:5555"
    }
    if botName == "" {
        botName = "bot"
    }

    time.Sleep(3 * time.Second) // aguarda broker e servidores subirem

    context, _ := zmq.NewContext()
    socket, _ := context.NewSocket(zmq.REQ)
    defer socket.Close()
    socket.Connect(brokerAddr)

    // Login
    for {
        resp, err := send(socket, "login", map[string]interface{}{"username": botName})
        if err != nil {
            fmt.Println("[CLIENT] Error on login:", err)
            time.Sleep(1 * time.Second)
            continue
        }
        if resp.Payload["status"] == "ok" {
            break
        }
        fmt.Println("[CLIENT] Login failed, retrying...")
        time.Sleep(1 * time.Second)
    }

    // Listar canais
    resp, err := send(socket, "list_channels", map[string]interface{}{})
    if err == nil && resp.Payload["status"] == "ok" {
        fmt.Printf("[CLIENT:%s] Available channels: %v\n", botName, resp.Payload["channels"])
    }

    // Criar canal
    channels := []string{"general", "random", "news"}
    channel := channels[rand.Intn(len(channels))]
    send(socket, "create_channel", map[string]interface{}{
        "username": botName,
        "name":     channel,
    })

    // Listar canais novamente
    resp, err = send(socket, "list_channels", map[string]interface{}{})
    if err == nil && resp.Payload["status"] == "ok" {
        fmt.Printf("[CLIENT:%s] Available channels after creation: %v\n", botName, resp.Payload["channels"])
    }
}