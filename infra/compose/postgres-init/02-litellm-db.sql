-- #503/infra — banco isolado do LiteLLM proxy no MESMO Postgres compartilhado
-- (mesmo padrão do 01-hatchet-db.sql, Q1). O litellm_config.yaml referencia
-- general_settings.database_url: os.environ/DATABASE_URL — sem o database
-- 'litellm' existir, o proxy sobe com "No DB Connected" e /model/new não
-- persiste (Configurações → Modelos de IA grava no Postgres da app, mas o
-- proxy recusa o modelo). Roda só no init de volume novo; para volume
-- existente, ver infra/runbooks/litellm-db-setup.md (createdb manual).
SELECT 'CREATE DATABASE litellm'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'litellm')\gexec
