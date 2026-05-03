-- scripts/init_db.sql
-- Runs once when the Docker postgres container is first created.
-- Creates extensions and the shared schema.
-- Tenant schemas are created via the API / migration tooling.

-- Required extensions
CREATE EXTENSION IF NOT EXISTS "pgcrypto";    -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";   -- uuid_generate_v4() fallback
CREATE EXTENSION IF NOT EXISTS "pg_trgm";     -- trigram search on loan_name, borrower_name

-- Shared schema
CREATE SCHEMA IF NOT EXISTS shared;

-- Grant the app user access
GRANT USAGE ON SCHEMA shared TO lsp_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA shared TO lsp_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA shared TO lsp_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA shared GRANT ALL ON TABLES TO lsp_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA shared GRANT ALL ON SEQUENCES TO lsp_user;
