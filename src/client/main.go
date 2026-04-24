package main

import (
    "fmt"
    "math/rand"
    "os"
    "sync"
    "time"

    zmq "github.com/pebbe/zmq4"
    "github.com/vmihailenco/msgpack/v5"
)

type Message struct {
    Type         string                 `msgpack:"type"`
    Payload      map[string]interface{} `msgpack:"payload"`
    Timestamp    int64                  `msgpack:"timestamp"`
    LogicalClock int64                  `msgpack:"logical_clock"`
}

type Response struct {
    Type         string                 `msgpack:"type"`
    Payload      map[string]interface{} `msgpack:"payload"`
    Timestamp    int64                  `msgpack:"timestamp"`
    LogicalClock int64                  `msgpack:"logical_clock"`
}

var brokerAddr = os.Getenv("BROKER_ADDR")
var pubsubAddr = os.Getenv("PUBSUB_ADDR")
var botName = os.Getenv("BOT_NAME")

// Relógio lógico global do bot
var logicalClock int64 = 0
var clockMu sync.Mutex

func incrementClock() int64 {
    clockMu.Lock()
    defer clockMu.Unlock()
    logicalClock++
    return logicalClock
}

func updateClock(received int64) int64 {
    clockMu.Lock()
    defer clockMu.Unlock()
    if received > logicalClock {
        logicalClock = received
    }
    logicalClock++
    return logicalClock
}

func send(socket *zmq.Socket, msgType string, payload map[string]interface{}) (Response, error) {
    clk := incrementClock()
    msg := Message{
        Type:         msgType,
        Payload:      payload,
        Timestamp:    time.Now().Unix(),
        LogicalClock: clk,
    }
    raw, err := msgpack.Marshal(msg)
    if err != nil {
        return Response{}, err
    }
    fmt.Printf("[CLIENT:%s] SEND | type=%s | payload=%v | timestamp=%d | logical_clock=%d\n",
        botName, msgType, payload, msg.Timestamp, clk)

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

    newClk := updateClock(resp.LogicalClock)
    fmt.Printf("[CLIENT:%s] RECV | type=%s | payload=%v | timestamp=%d | logical_clock=%d\n",
        botName, resp.Type, resp.Payload, resp.Timestamp, newClk)

    return resp, nil
}

func getChannels(socket *zmq.Socket) []string {
    resp, err := send(socket, "list_channels", map[string]interface{}{})
    if err != nil || resp.Payload["status"] != "ok" {
        return []string{}
    }
    raw, ok := resp.Payload["channels"]
    if !ok {
        return []string{}
    }
    iface, ok := raw.([]interface{})
    if !ok {
        return []string{}
    }
    channels := make([]string, 0, len(iface))
    for _, v := range iface {
        if s, ok := v.(string); ok {
            channels = append(channels, s)
        }
    }
    return channels
}

func subscriberLoop(subscribedChannels *[]string, mu *sync.Mutex) {
    context, _ := zmq.NewContext()
    sub, _ := context.NewSocket(zmq.SUB)
    defer sub.Close()
    sub.Connect(pubsubAddr)

    time.Sleep(2 * time.Second)

    subscribed := map[string]bool{}

    for {
        mu.Lock()
        channels := make([]string, len(*subscribedChannels))
        copy(channels, *subscribedChannels)
        mu.Unlock()

        for _, ch := range channels {
            if !subscribed[ch] {
                sub.SetSubscribe(ch)
                subscribed[ch] = true
                fmt.Printf("[CLIENT:%s] SUB | subscribed to channel=%s\n", botName, ch)
            }
        }

        sub.SetRcvtimeo(500 * time.Millisecond)
        parts, err := sub.RecvMessageBytes(0)
        if err != nil {
            continue
        }

        if len(parts) < 2 {
            continue
        }

        channel := string(parts[0])
        recvTs := time.Now().Unix()

        var pubMsg map[string]interface{}
        if err := msgpack.Unmarshal(parts[1], &pubMsg); err != nil {
            fmt.Printf("[CLIENT:%s] SUB | error decoding message on channel=%s\n", botName, channel)
            continue
        }

        username, _ := pubMsg["username"].(string)
        content, _ := pubMsg["content"].(string)

        var sendTs int64
        switch v := pubMsg["timestamp"].(type) {
        case int64:
            sendTs = v
        case uint64:
            sendTs = int64(v)
        case int32:
            sendTs = int64(v)
        case uint32:
            sendTs = int64(v)
        case int8:
            sendTs = int64(v)
        }

        var msgLogicalClock int64
        switch v := pubMsg["logical_clock"].(type) {
        case int64:
            msgLogicalClock = v
        case uint64:
            msgLogicalClock = int64(v)
        case int32:
            msgLogicalClock = int64(v)
        case uint32:
            msgLogicalClock = int64(v)
        case int8:
            msgLogicalClock = int64(v)
        }

        newClk := updateClock(msgLogicalClock)

        fmt.Printf(
            "[CLIENT:%s] SUB RECV | channel=%s | from=%s | content=%s | sent_ts=%d | recv_ts=%d | logical_clock=%d\n",
            botName, channel, username, content, sendTs, recvTs, newClk,
        )
    }
}

var adjectives = []string{"quick", "lazy", "happy", "sad", "bright", "dark", "fast", "slow", "warm", "cold"}
var nouns = []string{"fox", "dog", "cat", "bird", "fish", "bear", "wolf", "lion", "tiger", "eagle"}

func randomMessage() string {
    adj := adjectives[rand.Intn(len(adjectives))]
    noun := nouns[rand.Intn(len(nouns))]
    num := rand.Intn(1000)
    return fmt.Sprintf("The %s %s says hello #%d", adj, noun, num)
}

func main() {
    if brokerAddr == "" {
        brokerAddr = "tcp://broker:5555"
    }
    if pubsubAddr == "" {
        pubsubAddr = "tcp://pubsub_proxy:5558"
    }
    if botName == "" {
        botName = "bot"
    }

    rand.Seed(time.Now().UnixNano())
    time.Sleep(3 * time.Second)

    context, _ := zmq.NewContext()
    socket, _ := context.NewSocket(zmq.REQ)
    defer socket.Close()
    socket.Connect(brokerAddr)

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

    initialChannels := getChannels(socket)

    subscribedChannels := []string{}
    var mu sync.Mutex

    mu.Lock()
    for _, ch := range initialChannels {
        if len(subscribedChannels) >= 3 {
            break
        }
        subscribedChannels = append(subscribedChannels, ch)
    }
    mu.Unlock()

    go subscriberLoop(&subscribedChannels, &mu)

    allChannelNames := []string{"general", "random", "news", "tech", "sports", "music", "movies", "books", "travel", "food"}

    for {
        channels := getChannels(socket)
        if len(channels) < 5 {
            for _, name := range allChannelNames {
                exists := false
                for _, ch := range channels {
                    if ch == name {
                        exists = true
                        break
                    }
                }
                if !exists {
                    resp, err := send(socket, "create_channel", map[string]interface{}{
                        "username": botName,
                        "name":     name,
                    })
                    if err == nil && resp.Payload["status"] == "ok" {
                        fmt.Printf("[CLIENT:%s] Created channel: %s\n", botName, name)
                    }
                    break
                }
            }
            channels = getChannels(socket)
        }

        mu.Lock()
        currentSubs := len(subscribedChannels)
        mu.Unlock()

        if currentSubs < 3 && len(channels) > 0 {
            for _, ch := range channels {
                alreadySub := false
                mu.Lock()
                for _, s := range subscribedChannels {
                    if s == ch {
                        alreadySub = true
                        break
                    }
                }
                if !alreadySub {
                    subscribedChannels = append(subscribedChannels, ch)
                }
                mu.Unlock()
                if !alreadySub {
                    break
                }
            }
        }

        channels = getChannels(socket)
        if len(channels) == 0 {
            time.Sleep(1 * time.Second)
            continue
        }

        chosenChannel := channels[rand.Intn(len(channels))]

        for i := 0; i < 10; i++ {
            content := randomMessage()
            resp, err := send(socket, "publish", map[string]interface{}{
                "username": botName,
                "channel":  chosenChannel,
                "content":  content,
            })
            if err != nil {
                fmt.Printf("[CLIENT:%s] Error publishing: %v\n", botName, err)
            } else {
                fmt.Printf("[CLIENT:%s] Published to channel=%s | status=%v\n", botName, chosenChannel, resp.Payload["status"])
            }
            time.Sleep(1 * time.Second)
        }
    }
}