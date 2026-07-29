-- E05/infra — banco isolado do Hatchet no MESMO Postgres compartilhado (Q1).
-- Isolamento por database (não schema): hatchet-lite roda suas próprias goose
-- migrations e gerencia o próprio pool. Roda só no init de volume novo; para
-- volume existente, ver infra/runbooks/hatchet-setup.md (createdb manual).
SELECT 'CREATE DATABASE hatchet'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'hatchet')\gexec
