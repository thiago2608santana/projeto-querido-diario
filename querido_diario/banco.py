"""Persistência no Neon e busca lexical dos trechos em português."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .tipos import Conexao, Recorte, Registro, Trecho

SCHEMA = Path(__file__).with_name("schema.sql")
REFERENCIA = """
    t.id AS trecho_id, t.ordem, e.id AS edicao_id, e.municipio_id, e.municipio,
    e.uf, e.publicada_em, e.numero, e.extra, e.url_original, e.url_texto,
    e.original_caminho, e.texto_caminho, e.texto_origem, e.pdf_disponivel,
    CASE WHEN e.pdf_storage_sha256 = e.original_sha256 THEN e.pdf_storage_bucket END AS pdf_storage_bucket,
    CASE WHEN e.pdf_storage_sha256 = e.original_sha256 THEN e.pdf_storage_key END AS pdf_storage_key
"""


def conectar(dsn: str) -> Conexao:
    """Aceita URLs do Neon e o prefixo postgres do dialeto Psycopg no SQLAlchemy."""
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    return psycopg.connect(dsn, connect_timeout=15, autocommit=True, row_factory=dict_row)


def iniciar(db: Conexao) -> None:
    """Cria as tabelas em uma transação, sem tocar em outros esquemas."""
    with db.transaction():
        db.execute(SCHEMA.read_text(encoding="utf-8"), prepare=False)


@contextmanager
def exclusivo(db: Conexao, tarefa: str) -> Iterator[None]:
    """Mantém um bloqueio transacional, compatível com o pooler do Neon.

    Args:
        db: Conexão usada também nas operações protegidas pelo bloqueio.
        tarefa: Identidade da edição ou perfil que não deve executar em paralelo.

    Yields:
        Controle ao bloco protegido; confirma a transação ao sair sem erro.

    Raises:
        RuntimeError: Outra transação já possui o bloqueio. Não aguarda sua liberação.
        psycopg.Error: Falha no banco; a transação é revertida ao sair com erro.
    """
    with db.transaction():
        row = db.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS adquirido",
            ("querido_diario:" + tarefa,),
        ).fetchone()
        if not row or not row["adquirido"]:
            raise RuntimeError("Já existe uma execução concorrente para este item")
        yield


def salvar(db: Conexao, registro: Registro, trechos: Sequence[Trecho] | None) -> None:
    """Atualiza metadados e substitui trechos na mesma transação quando necessário.

    Args:
        db: Conexão com o esquema já preparado.
        registro: Colunas da edição, incluindo metadados serializáveis em JSONB.
        trechos: Novos trechos ordenados; ``None`` preserva os existentes e uma
            sequência vazia os remove. A troca elimina confirmações de vetores
            no Neon por cascata, mas não remove vetores remotos do Pinecone.

    Raises:
        psycopg.Error: Falha de persistência; metadados e trechos são revertidos juntos.
    """
    valores = {**registro, "metadados": Jsonb(registro["metadados"])}
    with db.transaction():
        db.execute(
            """
            INSERT INTO querido_diario.edicoes (
                id, municipio_id, municipio, uf, publicada_em, coletada_na_fonte_em,
                numero, extra, checksum_fonte, url_original, url_texto, metadados,
                original_sha256, original_caminho, original_tipo, original_bytes,
                texto_sha256, texto_caminho, texto_origem
            ) VALUES (
                %(id)s, %(municipio_id)s, %(municipio)s, %(uf)s, %(publicada_em)s,
                %(coletada_na_fonte_em)s, %(numero)s, %(extra)s, %(checksum_fonte)s,
                %(url_original)s, %(url_texto)s, %(metadados)s, %(original_sha256)s,
                %(original_caminho)s, %(original_tipo)s, %(original_bytes)s,
                %(texto_sha256)s, %(texto_caminho)s, %(texto_origem)s
            ) ON CONFLICT (id) DO UPDATE SET
                municipio = EXCLUDED.municipio, uf = EXCLUDED.uf,
                coletada_na_fonte_em = EXCLUDED.coletada_na_fonte_em,
                numero = EXCLUDED.numero, extra = EXCLUDED.extra,
                checksum_fonte = EXCLUDED.checksum_fonte,
                url_original = EXCLUDED.url_original, url_texto = EXCLUDED.url_texto,
                metadados = EXCLUDED.metadados, original_sha256 = EXCLUDED.original_sha256,
                original_caminho = EXCLUDED.original_caminho, original_tipo = EXCLUDED.original_tipo,
                original_bytes = EXCLUDED.original_bytes, texto_sha256 = EXCLUDED.texto_sha256,
                texto_caminho = EXCLUDED.texto_caminho, texto_origem = EXCLUDED.texto_origem,
                atualizada_em = now()
        """,
            valores,
        )
        if trechos is not None:
            db.execute("DELETE FROM querido_diario.trechos WHERE edicao_id = %s", (registro["id"],))
            with db.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO querido_diario.trechos (id, edicao_id, ordem, texto)
                    VALUES (%s, %s, %s, %s)
                """,
                    trechos,
                )


def filtros(municipio: str | None, inicio: date | None, fim: date | None) -> Recorte:
    """Fornece filtros tipados compartilhados entre busca lexical e vetorial."""
    return {"municipio": municipio, "inicio": inicio, "fim": fim}


RECORTE = """
    (%(municipio)s::text IS NULL OR e.municipio_id = %(municipio)s)
    AND (%(inicio)s::date IS NULL OR e.publicada_em >= %(inicio)s)
    AND (%(fim)s::date IS NULL OR e.publicada_em <= %(fim)s)
"""


def lexical(db: Conexao, consulta: str, recorte: Recorte, limite: int) -> list[Registro]:
    """Classifica com ts_rank_cd e extrai evidência só dos resultados finais."""
    return db.execute(
        f"""
        WITH q AS (SELECT websearch_to_tsquery('pg_catalog.portuguese', %(consulta)s) AS termo),
        ranked AS MATERIALIZED (
            SELECT t.id, ts_rank_cd(t.busca, q.termo) AS score
            FROM querido_diario.trechos t JOIN querido_diario.edicoes e ON e.id = t.edicao_id
            CROSS JOIN q WHERE t.busca @@ q.termo AND ({RECORTE})
            ORDER BY score DESC, e.publicada_em DESC, t.id LIMIT %(limite)s
        )
        SELECT {REFERENCIA}, ranked.score,
            ts_headline('pg_catalog.portuguese', t.texto, q.termo,
                        'MaxFragments=2, MinWords=12, MaxWords=40, StartSel=[[, StopSel=]]') AS evidencia
        FROM ranked JOIN querido_diario.trechos t ON t.id = ranked.id
        JOIN querido_diario.edicoes e ON e.id = t.edicao_id CROSS JOIN q
        ORDER BY ranked.score DESC, e.publicada_em DESC, t.id
    """,
        dict(recorte, consulta=consulta, limite=limite),
    ).fetchall()


def recuperar(db: Conexao, ids: list[str], recorte: Recorte) -> dict[str, Registro]:
    """Recupera texto e citações no Neon e descarta IDs que já não existem."""
    if not ids:
        return {}
    rows = db.execute(
        f"""
        SELECT {REFERENCIA}, t.texto AS evidencia
        FROM querido_diario.trechos t JOIN querido_diario.edicoes e ON e.id = t.edicao_id
        WHERE t.id = ANY(%(ids)s) AND ({RECORTE})
    """,
        dict(recorte, ids=ids),
    ).fetchall()
    return {row["trecho_id"]: row for row in rows}


def pendentes(db: Conexao, perfil: str, limite: int) -> list[Registro]:
    """Seleciona trechos que ainda não tiveram upsert confirmado neste perfil."""
    return db.execute(
        """
        SELECT t.id, t.texto, e.municipio_id, e.publicada_em
        FROM querido_diario.trechos t JOIN querido_diario.edicoes e ON e.id = t.edicao_id
        WHERE NOT EXISTS (SELECT 1 FROM querido_diario.vetores v
                          WHERE v.perfil = %s AND v.trecho_id = t.id)
        ORDER BY t.id LIMIT %s
    """,
        (perfil, limite),
    ).fetchall()


def confirmar(db: Conexao, perfil: str, ids: list[str]) -> None:
    """Registra sucesso sem erro se um trecho tiver sido substituído neste intervalo."""
    with db.transaction(), db.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO querido_diario.vetores (perfil, trecho_id)
            SELECT %s, id FROM querido_diario.trechos WHERE id = %s FOR KEY SHARE
            ON CONFLICT DO NOTHING
        """,
            [(perfil, item) for item in ids],
        )


def pdf_para_envio(db: Conexao, edicao_id: str) -> Registro | None:
    """Busca a referência local e o estado da última cópia confirmada."""
    return db.execute(
        """
        SELECT pdf_disponivel, original_caminho, original_sha256, pdf_storage_endpoint,
               pdf_storage_bucket, pdf_storage_key, pdf_storage_sha256
        FROM querido_diario.edicoes WHERE id = %s
    """,
        (edicao_id,),
    ).fetchone()


def pdfs_pendentes(
    db: Conexao,
    endpoint: str,
    bucket: str,
    recorte: Recorte,
    depois: str,
    limite: int,
) -> list[Registro]:
    """Pagina por ID para que uma falha não impeça o envio das demais edições."""
    return db.execute(
        f"""
        SELECT e.id FROM querido_diario.edicoes e
        WHERE e.pdf_disponivel AND e.id > %(depois)s AND ({RECORTE})
          AND (e.pdf_storage_endpoint IS DISTINCT FROM %(endpoint)s
            OR e.pdf_storage_bucket IS DISTINCT FROM %(bucket)s OR e.pdf_storage_key IS NULL
            OR e.pdf_storage_sha256 IS DISTINCT FROM e.original_sha256)
        ORDER BY e.id LIMIT %(limite)s
    """,
        dict(recorte, endpoint=endpoint, bucket=bucket, depois=depois, limite=limite),
    ).fetchall()


def confirmar_pdf(
    db: Conexao,
    edicao_id: str,
    endpoint: str,
    bucket: str,
    key: str,
    checksum: str,
) -> None:
    """Registra a cópia somente após a confirmação pelo Object Storage."""
    db.execute(
        """
        UPDATE querido_diario.edicoes
        SET pdf_storage_endpoint = %s, pdf_storage_bucket = %s, pdf_storage_key = %s,
            pdf_storage_sha256 = %s, pdf_storage_enviado_em = now()
        WHERE id = %s
    """,
        (endpoint, bucket, key, checksum, edicao_id),
    )
