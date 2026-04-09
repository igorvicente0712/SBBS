# SBBS - Simple Bulletin Board System

Este repositório contém o projeto da disciplina CC7261 - SISTEMAS DISTRIBUIDOS do curso de Ciência da Computação da FEI.

Neste, é desenvolvido um sistema simples de Bulletin Board System conforme as especificações do arquivo docs/enunciado.md. utilizando as linguagens Python (server) e Go (clientes).

O projeto é desenvolvido no ambiente do Github Codespaces com a utilização de Docker para replicação. As imagens utilizadas se encontram no Docker Hub.

## Integrantes
Igor Vicente Cutalo - R.A. 22.123.062-6

## Branches

- `main`: enunciados, documentação geral, estrutura do projeto.
- `parte01_request-reply`: implementação da Parte 1 - Request-Reply.

## Execução

```bash
docker-compose up
```

## Escolhas técnicas

### Linguagens

O broker e os servidores foram implementados em **Python**, pela facilidade de integração com ZeroMQ e SQLite. Os clientes foram implementados em **Go**, demonstrando interoperabilidade entre linguagens distintas através do broker. Em ambos os casos, a facilidade de uso e conhecimento prévio do autor influenciou fortemente na decisão.

### Comunicação e Serialização

A comunicação entre os componentes utiliza **ZeroMQ** com o padrão **ROUTER/DEALER**, que permite múltiplos clientes e servidores conectados simultaneamente através de um único broker. As mensagens são serializadas em **MessagePack (msgpack)**, formato binário mais eficiente que JSON em termos de tamanho e velocidade de serialização, com suporte disponível tanto para Python quanto para Go.

### Persistência

A persistência dos dados é feita com **SQLite**, armazenado em um volume Docker compartilhado entre os dois servidores (`/data/shared.db`). Essa abordagem garante consistência dos canais e mensagens independentemente de qual servidor atende cada requisição.