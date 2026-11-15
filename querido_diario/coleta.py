"""Registra cada edição e gera trechos pesquisáveis a partir do melhor texto disponível."""

import hashlib
import logging
import textwrap
from collections.abc import Iterator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import httpx

from . import banco
from .fonte import Arquivo, Fonte, arquivar, carregar_texto, sha256, validar_edicao
from .tipos import Conexao, EdicaoFonte, Registro, ResultadoColeta

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter

LOG = logging.getLogger(__name__)
CHUNK = 1800
OVERLAP = 240


def dividir(texto: str) -> Iterator[str]:
    """Gera trechos de até 1.800 caracteres com até 240 de sobreposição.

    Normaliza espaços e evita começar a margem no meio de uma palavra. A ordem
    produzida é a do texto, não a numeração de páginas do documento original.

    Args:
        texto: Conteúdo integral obtido do TXT ou da extração do PDF.

    Yields:
        Segmentos na ordem de leitura; entrada vazia não produz segmentos.
    """
    partes = textwrap.wrap(
        " ".join(texto.split()),
        width=CHUNK - OVERLAP,
        break_on_hyphens=False,
    )
    anterior = ""
    for parte in partes:
        margem = anterior[-OVERLAP:]
        if len(anterior) > OVERLAP:
            _, _, margem = margem.partition(" ")
        yield (margem + " " + parte).strip()
        anterior = parte


def chave_edicao(row: EdicaoFonte) -> str:
    """Distingue reedições por checksum ou, quando ausente, por URL."""
    material = "\n".join(
        str(v or "")
        for v in (
            row["territory_id"],
            row["date"],
            row.get("edition"),
            row.get("is_extra_edition"),
            row.get("file_checksum") or row["url"],
        )
    )
    return sha256(material.encode())


def _existente(
    raiz: Path,
    anterior: Registro,
    prefixo: Literal["original", "texto"],
) -> Arquivo | None:
    """Reutiliza somente arquivos dentro do acervo cujo SHA-256 confere."""
    caminho = anterior.get(prefixo + "_caminho")
    checksum = anterior.get(prefixo + "_sha256")
    if not caminho or not checksum:
        return None
    raiz = raiz.resolve()
    path = (raiz / caminho).resolve()
    if not path.is_relative_to(raiz):
        raise ValueError("Caminho do arquivo fora do acervo")
    try:
        with path.open("rb") as arquivo:
            tamanho = path.stat().st_size
            if not tamanho or (prefixo == "original" and tamanho != anterior.get("original_bytes")):
                return None
            if hashlib.file_digest(arquivo, "sha256").hexdigest() != checksum:
                return None
    except FileNotFoundError:
        return None
    return Arquivo(
        anterior[prefixo + "_sha256"],
        caminho,
        anterior["original_tipo"] if prefixo == "original" else "text/plain",
        tamanho,
    )


def _baixar(
    fonte: Fonte,
    url: str | None,
    raiz: Path,
    texto: bool,
    erros: list[str],
) -> Arquivo | None:
    """Registra falhas transitórias; arquivos 404/410 permanecem como ausentes."""
    if not url:
        return None
    try:
        return fonte.baixar(url, raiz, texto=texto)
    except (httpx.HTTPError, ValueError) as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        LOG.warning(
            "%s indisponível (%s).", "TXT" if texto else "Original", status or type(exc).__name__
        )
        if status not in (404, 410):
            erros.append("TXT" if texto else "original")
        return None


def _docling(
    converter: "DocumentConverter",
    raiz: Path,
    original: Arquivo,
    erros: list[str],
) -> Arquivo | None:
    """Extrai Markdown do PDF e registra falha sem descartar os metadados."""
    try:
        resultado = converter.convert(raiz / original.caminho)
        texto = resultado.document.export_to_markdown()
        if texto.strip():
            return arquivar(raiz, [texto.encode()], texto=True)
    except Exception as exc:
        LOG.warning("Falha na extração com Docling (%s).", type(exc).__name__)
    erros.append("Docling")
    return None


def coletar_edicao(
    db: Conexao,
    fonte: Fonte,
    raiz: Path,
    row: EdicaoFonte,
    converter: "DocumentConverter | None" = None,
) -> ResultadoColeta:
    """Baixa arquivos faltantes ou corrompidos e atualiza texto e metadados.

    O chamador deve manter ``banco.exclusivo`` para a edição durante a operação.
    Falhas de download preservam referências e trechos anteriores. Os arquivos
    são publicados antes da transação SQL; uma repetição reutiliza seu checksum.

    Args:
        db: Conexão com esquema preparado e bloqueio da edição ativo.
        fonte: Cliente para baixar os arquivos autorizados.
        raiz: Diretório do acervo local.
        row: Registro da fonte; campos extras são preservados nos metadados.
        converter: Extrator Docling opcional para PDFs sem TXT utilizável.

    Returns:
        Disponibilidade local do PDF e do texto e etapas que falharam.

    Raises:
        ValueError: Edição inválida ou caminho registrado fora do acervo.
        OSError: Falha de acesso ao acervo.
        psycopg.Error: Falha na leitura ou persistência da edição no banco.
    """
    row = validar_edicao(row)
    chave = chave_edicao(row)
    anterior = (
        db.execute("SELECT * FROM querido_diario.edicoes WHERE id = %s", (chave,)).fetchone() or {}
    )
    erros: list[str] = []
    original = _existente(raiz, anterior, "original")
    if original is None:
        original = _baixar(fonte, row["url"], raiz, False, erros)
    texto = _existente(raiz, anterior, "texto")
    origem = anterior.get("texto_origem") if texto else None
    if texto is None or (origem == "docling" and row.get("txt_url")):
        txt = _baixar(fonte, row.get("txt_url"), raiz, True, erros)
        if txt:
            texto, origem = txt, "querido_diario"
    if texto is None and original and original.tipo == "application/pdf" and converter:
        texto = _docling(converter, raiz, original, erros)
        origem = "docling" if texto else None
    if texto:
        novos = None
        if texto.sha256 != anterior.get("texto_sha256"):
            conteudo = carregar_texto(raiz / texto.caminho)
            novos = [
                (sha256(f"{chave}:{texto.sha256}:v1:{i}".encode()), chave, i, parte)
                for i, parte in enumerate(dividir(conteudo), 1)
            ]
    else:
        # Mantém trechos já gravados se o acervo local foi movido ou o download falhar.
        novos = None
        origem = anterior.get("texto_origem")
    quando = row.get("scraped_at")
    data_coleta = datetime.fromisoformat(quando) if quando else None
    if data_coleta and data_coleta.tzinfo is None:
        data_coleta = data_coleta.replace(tzinfo=timezone.utc)
    registro = dict(
        id=chave,
        municipio_id=str(row["territory_id"]),
        municipio=row.get("territory_name") or str(row["territory_id"]),
        uf=row.get("state_code"),
        publicada_em=date.fromisoformat(row["date"]),
        coletada_na_fonte_em=data_coleta,
        numero=str(row["edition"]) if row.get("edition") is not None else None,
        extra=row.get("is_extra_edition"),
        checksum_fonte=row.get("file_checksum"),
        url_original=row["url"],
        url_texto=row.get("txt_url"),
        metadados=row,
        original_sha256=original.sha256 if original else anterior.get("original_sha256"),
        original_caminho=original.caminho if original else anterior.get("original_caminho"),
        original_tipo=original.tipo if original else anterior.get("original_tipo"),
        original_bytes=original.tamanho if original else anterior.get("original_bytes"),
        texto_sha256=texto.sha256 if texto else anterior.get("texto_sha256"),
        texto_caminho=texto.caminho if texto else anterior.get("texto_caminho"),
        texto_origem=origem,
    )
    if novos is not None or any(anterior.get(k) != v for k, v in registro.items()):
        banco.salvar(db, registro, novos)
    return {
        "pdf": bool(original and original.tipo == "application/pdf"),
        "texto": bool(texto),
        "falhas": erros,
    }
