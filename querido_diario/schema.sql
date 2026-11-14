-- O esquema dedicado permite usar um banco que já contém outras aplicações.
CREATE SCHEMA IF NOT EXISTS querido_diario;

CREATE TABLE IF NOT EXISTS querido_diario.edicoes (
    id text PRIMARY KEY,
    municipio_id text NOT NULL,
    municipio text NOT NULL,
    uf text,
    publicada_em date NOT NULL,
    coletada_na_fonte_em timestamptz,
    numero text,
    extra boolean,
    checksum_fonte text,
    url_original text NOT NULL,
    url_texto text,
    metadados jsonb NOT NULL,
    original_sha256 text,
    original_caminho text,
    original_tipo text,
    original_bytes bigint,
    texto_sha256 text,
    texto_caminho text,
    texto_origem text,
    pdf_disponivel boolean GENERATED ALWAYS AS
        (coalesce(original_tipo = 'application/pdf', false)) STORED,
    atualizada_em timestamptz NOT NULL DEFAULT now()
);
-- Aplica a migração também a bancos criados antes da integração S3.
ALTER TABLE querido_diario.edicoes
    ADD COLUMN IF NOT EXISTS pdf_storage_endpoint text,
    ADD COLUMN IF NOT EXISTS pdf_storage_bucket text,
    ADD COLUMN IF NOT EXISTS pdf_storage_key text,
    ADD COLUMN IF NOT EXISTS pdf_storage_sha256 text,
    ADD COLUMN IF NOT EXISTS pdf_storage_enviado_em timestamptz;

CREATE INDEX IF NOT EXISTS qd_edicoes_municipio_data
    ON querido_diario.edicoes (municipio_id, publicada_em);

-- O texto indexado fica em trechos. A cópia integral fica no acervo local.
CREATE TABLE IF NOT EXISTS querido_diario.trechos (
    id text PRIMARY KEY,
    edicao_id text NOT NULL REFERENCES querido_diario.edicoes(id) ON DELETE CASCADE,
    ordem integer NOT NULL,
    texto text NOT NULL,
    busca tsvector GENERATED ALWAYS AS
        (to_tsvector('pg_catalog.portuguese'::regconfig, texto)) STORED,
    UNIQUE (edicao_id, ordem)
);
CREATE INDEX IF NOT EXISTS qd_trechos_busca
    ON querido_diario.trechos USING gin (busca);

-- Só confirma a indexação após a resposta do Pinecone.
CREATE TABLE IF NOT EXISTS querido_diario.vetores (
    perfil text NOT NULL,
    trecho_id text NOT NULL REFERENCES querido_diario.trechos(id) ON DELETE CASCADE,
    indexado_em timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (perfil, trecho_id)
);
