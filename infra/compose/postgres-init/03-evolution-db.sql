-- Evolution API v2 — banco isolado no MESMO Postgres compartilhado
-- (mesmo padrão do 01-hatchet-db.sql e 02-litellm-db.sql, Q1).
--
-- Por que existe: a v2 da Evolution NÃO roda sem banco. O entrypoint da imagem
-- executa `prisma migrate deploy` ANTES de subir a API, usando DATABASE_PROVIDER
-- para escolher o schema; sem provider ele morre em `Error: Database provider
-- invalid.` e o container entra em crash-loop. O `DATABASE_ENABLED: "false"`
-- que este compose carregava é herança da v1 e não isenta a migração.
--
-- Medido em 31/08/2026: o sandbox-os estava nesse crash-loop desde 29/07 —
-- 47.316 reinícios, NENHUMA mensagem de WhatsApp entregue no período. O envio
-- do agente falhava com 503 PROVIDER_UNAVAILABLE e o run seguia completo, o
-- que tornou a falha silenciosa por um mês.
--
-- Roda só no init de volume NOVO; para volume existente, ver
-- infra/runbooks/evolution-db-setup.md (createdb manual).
SELECT 'CREATE DATABASE evolution'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'evolution')\gexec
