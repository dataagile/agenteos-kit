# Kit de distribuição AgenteOS

Kit **pull-only**: nenhuma imagem é construída aqui, todas vêm prontas do Docker
Hub (org `dataagileai`). Gerado automaticamente por
`scripts/gen_distrib.py` — **não edite os `.yml` deste diretório à mão**; eles
são sobrescritos a cada `make kit`.

## Pré-requisitos

- Docker Engine + Docker Compose v2 (`docker compose version`).
- VPS/servidor **amd64** (as imagens publicadas são amd64; não há build local
  para compensar arquitetura).
- Portas livres no host: `80`/`443` (produção) ou `8090`/`8100`/`8889` (sandbox).

## Instalação rápida (recomendada)

```bash
./install.sh            # pergunta o que instalar (prod, sandbox ou both)
```

O instalador verifica os pré-requisitos, gera os segredos automaticamente
(`openssl`), sobe a stack e executa o passo do token do Hatchet sozinho. Se o
`.env` já existe, ele é preservado (re-rodar é seguro). Os passos manuais
abaixo continuam válidos para quem prefere controle total.

## Instalação — produção

```bash
cp .env.example .env
# editar .env: preencher todo valor "trocar-obrigatoriamente" com um segredo real
# (SECRET_KEY, APP_MFA_MASTER_KEY, JWT_INTERNAL_SECRET, INTERNAL_TOKEN,
#  SERVICE_TOKEN, SHORT_LINK_HMAC_SECRET: gerar cada um com `openssl rand -hex 32`)

docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

A aplicação sobe em `http://<host>` (porta `HTTP_PORT`, padrão `80`).

## Instalação — sandbox

Mesma stack, isolada por outro nome de projeto (`agenteos-sandbox`) e outras
portas — pode rodar na **mesma VPS** que a produção sem conflito.

```bash
cp .env.sandbox.example .env.sandbox
# editar .env.sandbox: segredos DIFERENTES dos de produção (ver aviso no arquivo)

docker compose -f docker-compose.sandbox.yml --env-file .env.sandbox pull
docker compose -f docker-compose.sandbox.yml --env-file .env.sandbox up -d
```

O servidor MCP (autoria de agentes via Claude Code/skill) **nasce desligado** e a
porta 8100 fica em loopback — o acesso externo é pela rota `/mcp` do nginx, com TLS
(`https://<dominio-do-sandbox>/mcp`). Para habilitar, siga a seção "MCP — autoria de
agentes" do `.env.sandbox.example`: `MCP_ENABLED=true`, `MCP_ALLOWED_TENANT_IDS` com o
UUID do tenant (existe só APÓS o provisionamento pelo hub), `MCP_ADMIN_TENANT_ID` e
`MCP_ADMIN_KEY` (chave `keys.admin` emitida via `issue-key` — sem ela a página
Configurações → Chaves MCP responde erro). Chaves de AUTORIA para desenvolvedores
usam os 6 scopes `spec.list spec.read spec.node_types spec.validate spec.write
spec.publish`. Num deploy via painel (Dokploy etc.), essas variáveis devem viver no
painel — arquivo `.env` editado à mão é regenerado no próximo deploy.

### Token do Hatchet (automático)

O token dos workers é gerado automaticamente no primeiro `up -d` pelo serviço
`hatchet-token-init` (one-shot): ele autentica na API do Hatchet com o
`HATCHET_ADMIN_EMAIL/PASSWORD` do seu `.env`, cria o token e o grava num volume
que os workers leem sozinhos. Rotação também é automática (regenera quando
faltam <30 dias de validade). Se você preferir controlar o token manualmente,
basta definir `HATCHET_CLIENT_TOKEN` no `.env` — a variável explícita sempre
vence o arquivo.

**Recovery** (só se o container do `hatchet` for recriado sem o volume de
config — chaves novas invalidam tokens antigos):

```bash
docker compose --env-file .env -f docker-compose.prod.yml run --rm -e HATCHET_TOKEN_FORCE=1 hatchet-token-init
docker compose --env-file .env -f docker-compose.prod.yml restart api-gateway-worker agent-runtime-worker hitl-service-worker evaluator-worker
```


## Instalação via painéis (Dokploy, Coolify, EasyPanel)

Se o servidor já roda um painel de deploy, você não precisa do `install.sh` —
o painel assume o papel dele. O kit é compose pull-only, exatamente o formato
que essas ferramentas consomem:

1. Crie um serviço do tipo **Docker Compose** apontando para este repositório
   (`https://github.com/tbc-servicos/agenteos-kit`), arquivo
   `docker-compose.prod.yml`. Para a sandbox, um segundo serviço com
   `docker-compose.sandbox.yml`.
2. Cole as variáveis do `.env.example` na tela de environment do painel,
   **gerando segredos reais** (instruções em cada linha do arquivo). Segredo
   com valor placeholder não sobe — o primeiro boot recusa e lista as
   variáveis pendentes.
3. Não esqueça `COMPOSE_PROFILES=runtime,hitl,evaluator` na environment — sem
   ele a stack sobe sem workers e nenhuma execução roda.
4. Deploy. Não há `build:` no kit — o painel só faz pull das imagens.
5. **Token do Hatchet**: automático — o serviço `hatchet-token-init` gera e
   distribui o token no primeiro deploy (nada a fazer no painel).

Notas por ferramenta:

- **Dokploy**: exponha o painel via porta do nginx (`HTTP_PORT`) ou aponte o
  proxy (Traefik/NPM) para o serviço `nginx` interno. Nunca rode deploy que
  recrie o container `hatchet` isoladamente.
- **Coolify**: marque o serviço `nginx` para receber o domínio/TLS automático
  (porta interna 80).
- **EasyPanel**: suporte a compose é mais limitado; se travar, o caminho
  `install.sh` via SSH funciona em qualquer VPS e ignora o painel.

## Upgrade

Os serviços da aplicação usam `pull_policy: always`: **todo `docker compose up -d`
já verifica e baixa a imagem `latest` mais nova** antes de subir.

```bash
docker compose -f docker-compose.prod.yml --env-file .env up -d
```

**`docker restart` NÃO atualiza nada** — restart reusa o container existente.
Atualização é sempre via `docker compose up -d`. (Se o Docker Hub estiver
inacessível no momento do `up`, rode com `--pull missing` para subir com a
imagem local: `docker compose up -d --pull missing`.)

Para o suporte confirmar qual versão do código está no ar:

```bash
docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' agenteos-prod-api-gateway-1
```

## Prod e sandbox no mesmo servidor: cuidado com `latest`

A tag `latest` de uma imagem é **única no daemon Docker do host** — ela não é
isolada por projeto/compose. Se produção e sandbox rodam no mesmo servidor e
ambas usam `IMAGE_TAG=latest`, um `pull` feito só no sandbox já atualiza o
ponteiro `latest` local; a próxima vez que a produção recriar um container
(deploy, reboot, `up -d` depois de qualquer mudança) ela sobe a imagem nova
**mesmo sem ter rodado `pull` na produção**.

Duas formas de evitar surpresa:

- **(a)** Atualize as duas instâncias juntas (mesmo `pull`/`up -d` nas duas, na
  mesma janela).
- **(b)** Fixe a produção numa tag imutável (`IMAGE_TAG=AAAA.MM.DD-<sha>` no
  `.env`) e promova conscientemente depois de validar no sandbox — só o
  sandbox fica em `latest`.

## Rollback

Cada publicação grava, além de `latest`, uma tag imutável
`AAAA.MM.DD-<sha-curto>`. Para fixar (ou reverter para) uma versão específica,
defina no `.env`/`.env.sandbox`:

```bash
IMAGE_TAG=2026.07.29-abc1234
```

e rode `pull && up -d` novamente.

## Avisos operacionais

- **Segredos placeholder não sobem**: no primeiro boot, o serviço `db-migrate`
  (porteiro da stack) recusa iniciar se qualquer segredo ainda estiver com o
  valor de exemplo (`trocar-...`) do `.env.example`, listando quais faltam
  trocar. Gere valores reais (instruções em cada linha do `.env.example`) e
  rode `docker compose up -d` novamente.
- **WhatsApp (Evolution API) vem desativado**: o profile `channels` fica fora
  do default porque o Evolution API exige configuração própria (banco/chave —
  vars `EVOLUTION_*`). Para ativar: configure as vars e acrescente `,channels`
  ao `COMPOSE_PROFILES`.
- **Não remova `COMPOSE_PROFILES` do `.env`**: o profile `runtime` contém os
  workers — sem eles a plataforma sobe "verde" mas nenhuma execução de agente
  roda (fica travada em "received"). `hitl` e `evaluator` também são parte do
  produto; só `retrieval` é opcional (fora deste kit v1).

- **Nunca recrie o container `hatchet` isoladamente** (`docker compose up
  --force-recreate hatchet` ou `rm` + `up`). As chaves de assinatura do
  Hatchet são geradas no boot e vivem só no volume `hatchet_config`; recriar o
  container sem preservar o volume invalida o token de **todos** os workers e
  trava runs em `received`. Use sempre `docker compose up -d` (sem
  `--force-recreate`) para atualizar os demais serviços.
- **Sandbox: specs de agente sobrevivem a redeploy.** O store de AgentSpecs do
  `mcp-server` vive no volume durável `agent_specs_data` (compartilhado com o
  seed do `db-migrate`). Inclua esse volume na rotina de backup se a autoria
  for valiosa.
- **Produção (kit do cliente): agente novo chega por contrato, sem pull de
  imagem** (044-catalogo-hub-spoke-automatico). O `docker-compose.prod.yml`
  também ganhou o volume `agent_specs_data` — quando o hub cria/atualiza um
  contrato, o material de execução (spec + templates `.j2`) viaja junto no
  push e o `api-gateway` grava nesse volume; o `agent-runtime` e o
  `agent-runtime-worker` leem dali para executar o agente (nó
  `render_template` incluso). Sem ação do cliente: não precisa de
  `docker compose pull` nem de kit novo para um produto que o hub acabou de
  liberar. Inclua esse volume na rotina de backup pela mesma razão do
  sandbox.
- **Rate-limit de pull anônimo do Docker Hub**: se `docker compose pull`
  começar a falhar com `429 Too Many Requests`, rode `docker login` (mesmo
  sem conta paga — usuário autenticado tem limite maior que anônimo).
- **Profile `retrieval` (embeddings/reranker) fora deste kit v1**: esses dois
  serviços não têm imagem publicada (`build:` local no monorepo) — não
  suba o profile `retrieval` a partir deste kit.
