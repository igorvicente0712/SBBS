FROM golang:1.21
RUN apt-get update && apt-get install -y libzmq3-dev
WORKDIR /app
COPY . .
RUN go mod tidy
RUN go build -o client .
CMD ["./client"]