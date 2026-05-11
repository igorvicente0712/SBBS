# SBBS - Simple Bulletin Board System

Este repositório contém o projeto da disciplina CC7261 - SISTEMAS DISTRIBUIDOS do curso de Ciência da Computação da FEI.

Neste, é desenvolvido um sistema simples de Bulletin Board System conforme as especificações do arquivo docs/enunciado.md, utilizando as linguagens Python (servidor) e Go (clientes).

O projeto é desenvolvido no ambiente do Github Codespaces com a utilização de Docker para replicação. As imagens utilizadas se encontram no Docker Hub. Em caso das imagens se apresentarem indisponíveis, é necessário o build local.

## Integrantes
Igor Vicente Cutalo - R.A. 22.123.062-6

## Branches

- `main`: enunciados, documentação geral, estrutura do projeto.
- `parte01_request-reply`: implementação da Parte 1 - Request-Reply.
- `parte02_publish-subscribe`: implementação da Parte 2 - Publish-Subscribe.
- `parte03_relogios-heartbeat`: implementação da Parte 3 - Relógios e Heartbeat.
- `parte04_eleicao`: implementação da Parte 4 - Eleição.
- `parte05-consistencia_replicacao`: implementação da Parte 5 - Consistência e Replicação

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

---

### Parte 02 - Publish-Subscribe

Cada cliente, ao iniciar, busca os canais existentes e se inscreve em até 3 deles via `SetSubscribe` no socket ZeroMQ SUB. A goroutine de subscriber roda em paralelo ao loop principal, recebendo mensagens em tempo real com um timeout de 500ms para verificar periodicamente se há novos canais para assinar. O servidor, ao processar um `publish`, persiste a mensagem no banco e a encaminha ao proxy XSUB/XPUB, que a distribui a todos os clientes inscritos no respectivo canal.

---

### Parte 5 - Consistência e Replicação

#### **Método escolhido: Réplica Ativa com propagação por Push**

#### O problema

O broker distribui as requisições dos clientes entre os servidores usando round-robin. Com isso, cada servidor processa apenas uma parte das mensagens enviadas pelos bots. Se um servidor cair, parte do histórico se perde. Além disso, um cliente que consultar o histórico de um canal receberá apenas as mensagens que passaram por aquele servidor específico.

#### Como a Réplica Ativa resolve o problema

Na **réplica ativa**, todos os servidores mantêm cópias idênticas do estado e cada operação de escrita é aplicada em todas as réplicas. No modelo adotado neste projeto, o servidor que recebe uma operação de escrita (login, criação de canal ou publicação de mensagem) executa a operação localmente e, em seguida, envia a mesma operação para todos os seus peers via comunicação direta (ZeroMQ REQ/REP na porta 5570), para que eles também a apliquem em seus bancos de dados locais.

Isso garante que, ao final da propagação, todos os servidores possuam os mesmos dados, eliminando a perda de histórico causada pelo balanceamento de carga do broker.

#### Como foi implementado

Foram adicionadas duas funções principais ao `server.py`:

- `replicate_to_peers(peers, operation, data)`: chamada após cada escrita bem-sucedida, dispara em uma thread separada o envio da operação para cada peer via `send_to_peer`, com o tipo de mensagem `replicate`.

- `apply_replicated_operation(conn, operation, data)`: executada no `peer_server_loop` quando uma mensagem do tipo `replicate` é recebida. Aplica a operação diretamente no banco de dados local **sem re-replicar**, evitando loops infinitos.

Os handlers de escrita (`handle_login`, `handle_create_channel`, `handle_publish`) foram ajustados para retornar, além do resultado, um dicionário `replication_data` contendo os dados necessários para reproduzir a operação nos peers. Operações de leitura (`list_channels`, `get_messages`) não geram replicação.

A replicação é disparada de forma assíncrona (em thread daemon) para não bloquear a resposta ao cliente.

#### Consistência resultante

O modelo almeja **consistência forte**, pois a replicação é iniciada imediatamente após cada escrita. Porém, como a propagação ocorre de forma assíncrona em relação à resposta ao cliente, existe uma janela de tempo curta em que os servidores podem estar em estados divergentes. Na prática, o comportamento se aproxima de **consistência eventual forte**: as réplicas convergem rapidamente para o mesmo estado, e a ordem das mensagens é preservada pelo relógio lógico (Lamport) implementado nas partes anteriores.

#### Adaptações necessárias

A principal adaptação em relação ao método teórico foi a necessidade de distinguir entre uma operação originada por um cliente e uma operação originada por replicação. Isso foi resolvido pelo uso de um tipo de mensagem dedicado (`replicate`) no canal peer-to-peer, e pela lógica de `apply_replicated_operation` que não chama `replicate_to_peers` novamente, quebrando o ciclo.

Outra adaptação foi garantir que os dados replicados não fossem inseridos mais de uma vez: o uso de `INSERT OR IGNORE` para usuários e canais garante que uma operação duplicada (caso um peer a receba mais de uma vez) não cause erro nem dado inconsistente.