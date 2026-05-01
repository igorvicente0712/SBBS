# SBBS - Simple Bulletin Board System

Este repositório contém o projeto da disciplina CC7261 - SISTEMAS DISTRIBUIDOS do curso de Ciência da Computação da FEI.

Neste, é desenvolvido um sistema simples de Bulletin Board System conforme as especificações do arquivo docs/enunciado.md, utilizando as linguagens Python (servidor) e Go (clientes).

O projeto é desenvolvido no ambiente do Github Codespaces com a utilização de Docker para replicação. As imagens utilizadas se encontram no Docker Hub.

## Integrantes
Igor Vicente Cutalo - R.A. 22.123.062-6

## Branches

- `main`: enunciados, documentação geral, estrutura do projeto.
- `parte01_request-reply`: implementação da Parte 1 - Request-Reply.
- `parte02_publish-subscribe`: implementação da Parte 2 - Publish-Subscribe.
- `parte03_relogios-heartbeat`: implementação da Parte 3 - Relógios e Heartbeat.

## Execução

```bash
docker-compose up
```

## Escolhas técnicas

### Linguagens

O broker, os servidores e o proxy PubSub foram implementados em **Python**, pela facilidade de integração com ZeroMQ e SQLite. Os clientes foram implementados em **Go**, demonstrando interoperabilidade entre linguagens distintas através do broker. Em ambos os casos, a facilidade de uso e conhecimento prévio do autor influenciou fortemente na decisão.

### Comunicação e Serialização

A comunicação entre os componentes utiliza **ZeroMQ**. Para o canal de requisição/resposta, é usado o padrão **ROUTER/DEALER**, que permite múltiplos clientes e servidores conectados simultaneamente através de um único broker. Para o canal de publicação e entrega de mensagens, é usado o padrão **XSUB/XPUB** através de um proxy dedicado, que atua como intermediário entre os servidores (publicadores) e os clientes (assinantes), permitindo que múltiplos servidores publiquem e múltiplos clientes recebam mensagens sem acoplamento direto entre eles.

As mensagens são serializadas em **MessagePack (msgpack)**, formato binário mais eficiente que JSON em termos de tamanho e velocidade de serialização, com suporte disponível tanto para Python quanto para Go.

### Persistência

A persistência dos dados é feita com **SQLite**, armazenado em um volume Docker compartilhado entre os dois servidores (`/data/shared.db`). O banco é configurado com `journal_mode=WAL` (Write-Ahead Logging) e `busy_timeout=5000`, o que permite acesso concorrente seguro entre os servidores sem bloqueios. Todas as mensagens publicadas nos canais são persistidas antes de serem enviadas ao proxy PubSub, garantindo que o histórico esteja disponível mesmo para clientes que se conectem após a publicação, através do comando `get_messages`.

### Publish-Subscribe

Cada cliente, ao iniciar, busca os canais existentes e se inscreve em até 3 deles via `SetSubscribe` no socket ZeroMQ SUB. A goroutine de subscriber roda em paralelo ao loop principal, recebendo mensagens em tempo real com um timeout de 500ms para verificar periodicamente se há novos canais para assinar. O servidor, ao processar um `publish`, persiste a mensagem no banco e a encaminha ao proxy XSUB/XPUB, que a distribui a todos os clientes inscritos no respectivo canal.