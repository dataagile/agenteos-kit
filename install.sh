#!/usr/bin/env bash
# AgenteOS — instalador do kit de distribuição.
# Uso: ./install.sh            (pergunta o que instalar)
#      ./install.sh prod       ./install.sh sandbox       ./install.sh both
#
# O que faz: gera o .env com segredos reais (openssl), sobe a stack puxando as
# imagens do Docker Hub e executa o passo pós-boot do token do Hatchet (sem ele
# os workers ficam reiniciando). Idempotente por instância: se o .env já
# existe, ele NÃO é sobrescrito (para reinstalar do zero, apague-o antes).
set -euo pipefail
cd "$(dirname "$0")"

fail() { echo "ERRO: $*" >&2; exit 1; }

command -v docker >/dev/null || fail "docker não encontrado — instale o Docker Engine (https://docs.docker.com/engine/install/)"
docker compose version >/dev/null 2>&1 || fail "docker compose v2 não encontrado"
docker info >/dev/null 2>&1 || fail "daemon do Docker inacessível (permissão? serviço parado?)"
command -v openssl >/dev/null || fail "openssl não encontrado (necessário para gerar segredos)"

INSTANCE="${1:-}"
if [ -z "$INSTANCE" ]; then
    read -r -p "O que instalar? [prod/sandbox/both] (padrão: prod): " INSTANCE
    INSTANCE="${INSTANCE:-prod}"
fi
case "$INSTANCE" in prod|sandbox|both) ;; *) fail "opção inválida: $INSTANCE" ;; esac

read -r -p "Domínio público desta instalação (Enter para 'localhost'): " DOMAIN
DOMAIN="${DOMAIN:-localhost}"

# Troca todo valor placeholder (trocar-*/troque-me) por um segredo real.
gen_env() { # $1=exemplo $2=destino
    awk -v domain="$DOMAIN" '
        /^[A-Z_]+=(trocar-|troque-me)/ {
            split($0, kv, "=");
            cmd = "openssl rand -hex 24"; cmd | getline secret; close(cmd);
            print kv[1] "=" secret; next
        }
        /^INVITE_LINK_BASE=/ { print "INVITE_LINK_BASE=https://" domain; next }
        { print }
    ' "$1" > "$2"
    chmod 600 "$2"
}

# Espera um serviço ficar saudável no projeto.
wait_healthy() { # $1=envfile $2=composefile $3=serviço
    for _ in $(seq 1 60); do
        st=$(docker compose --env-file "$1" -f "$2" ps --format '{{.Health}}' "$3" 2>/dev/null || true)
        [ "$st" = "healthy" ] && return 0
        sleep 5
    done
    fail "$3 não ficou saudável em 5min — veja: docker compose --env-file $1 -f $2 logs $3"
}

install_instance() { # $1=prod|sandbox
    local envfile compose
    if [ "$1" = "prod" ]; then envfile=".env"; compose="docker-compose.prod.yml";
    else envfile=".env.sandbox"; compose="docker-compose.sandbox.yml"; fi

    echo "── [$1] preparando $envfile"
    if [ -f "$envfile" ]; then
        echo "── [$1] $envfile já existe — mantendo (segredos preservados)"
    else
        gen_env "${envfile}.example" "$envfile"
    fi

    echo "── [$1] subindo a stack (primeira vez baixa as imagens — pode demorar)"
    docker compose --env-file "$envfile" -f "$compose" up -d

    echo "── [$1] aguardando Hatchet e Postgres"
    wait_healthy "$envfile" "$compose" postgres
    wait_healthy "$envfile" "$compose" hatchet

    if grep -q '^HATCHET_CLIENT_TOKEN=.\+' "$envfile"; then
        echo "── [$1] HATCHET_CLIENT_TOKEN já configurado"
    else
        echo "── [$1] gerando token do Hatchet (passo único pós-boot)"
        local tid token
        tid=$(docker compose --env-file "$envfile" -f "$compose" exec -T postgres \
            psql -U "$(grep -oP '^POSTGRES_USER=\K.*' "$envfile" || echo agenteos)" -d hatchet \
            -t -A -c 'SELECT id FROM "Tenant" LIMIT 1;')
        [ -n "$tid" ] || fail "[$1] tenant do Hatchet não encontrado"
        token=$(docker compose --env-file "$envfile" -f "$compose" exec -T hatchet \
            /hatchet-admin token create --config /config --tenant-id "$tid" | tr -d '[:space:]')
        [ -n "$token" ] || fail "[$1] hatchet-admin token create não retornou token"
        if grep -q '^HATCHET_CLIENT_TOKEN=' "$envfile"; then
            sed -i "s|^HATCHET_CLIENT_TOKEN=.*|HATCHET_CLIENT_TOKEN=$token|" "$envfile"
        else
            echo "HATCHET_CLIENT_TOKEN=$token" >> "$envfile"
        fi
        # --no-deps: recriar o hatchet junto invalidaria o token recém-criado
        docker compose --env-file "$envfile" -f "$compose" up -d --no-deps \
            api-gateway-worker agent-runtime-worker hitl-service-worker evaluator-worker
    fi
    echo "── [$1] OK"
}

if [ "$INSTANCE" = "prod" ] || [ "$INSTANCE" = "both" ]; then install_instance prod; fi
if [ "$INSTANCE" = "sandbox" ] || [ "$INSTANCE" = "both" ]; then install_instance sandbox; fi

echo
echo "════════════════════════════════════════════════════════════"
echo " AgenteOS instalado."
[ "$INSTANCE" != "sandbox" ] && echo "  Painel produção:  http://$DOMAIN:$(grep -oP '^HTTP_PORT=\K.*' .env 2>/dev/null || echo 80)"
[ "$INSTANCE" != "prod" ] && { echo "  Painel sandbox:   http://$DOMAIN:$(grep -oP '^SANDBOX_HTTP_PORT=\K.*' .env.sandbox 2>/dev/null || echo 8090)"; echo "  MCP (autoria):    http://$DOMAIN:$(grep -oP '^MCP_PORT=\K.*' .env.sandbox 2>/dev/null || echo 8100)"; }
echo "  Upgrade:          docker compose --env-file <env> -f <compose> up -d"
echo "  Guarde os arquivos .env — eles contêm os segredos desta instalação."
echo "════════════════════════════════════════════════════════════"
