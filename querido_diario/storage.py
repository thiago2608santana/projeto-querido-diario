"""Copia PDFs locais para o Neon Object Storage e confirma a referência no Postgres."""

import hashlib
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from . import banco

LOG = logging.getLogger(__name__)
AWS_VARS = ("AWS_ENDPOINT_URL_S3", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION")


def raiz_acervo() -> Path:
    """Resolve a mesma pasta de acervo para coleta e envio de arquivos antigos."""
    return Path(os.getenv("QD_ACERVO", "data/acervo")).expanduser().resolve()


class StoragePDF:
    """Envia um PDF sob uma chave determinística derivada de seu conteúdo."""

    def __init__(self, client, endpoint: str, bucket: str):
        self.client = client
        self.endpoint = endpoint
        self.bucket = bucket

    @classmethod
    def do_ambiente(cls, *, obrigatorio=False):
        """Usa o bucket explícito ou descobre o destino se houver um único bucket."""
        config = {nome: os.getenv(nome, "").strip() for nome in AWS_VARS}
        bucket = os.getenv("QD_STORAGE_BUCKET", "").strip()
        if not obrigatorio and not bucket and not any(config.values()):
            return None
        ausentes = [nome for nome, valor in config.items() if not valor]
        if ausentes:
            raise ValueError("Configure o Object Storage: " + ", ".join(ausentes))
        endpoint = config["AWS_ENDPOINT_URL_S3"].rstrip("/")
        url = urlsplit(endpoint)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("AWS_ENDPOINT_URL_S3 deve ser um endpoint HTTPS sem credenciais ou parâmetros")
        client = boto3.client(
            "s3", endpoint_url=endpoint, region_name=config["AWS_REGION"],
            aws_access_key_id=config["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=config["AWS_SECRET_ACCESS_KEY"],
            config=Config(
                signature_version="s3v4", s3={"addressing_style": "path"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"mode": "standard", "total_max_attempts": 4},
                connect_timeout=10, read_timeout=60,
            ),
        )
        if not bucket:
            try:
                resposta = client.list_buckets()
            except ClientError as exc:
                raise ValueError("Configure QD_STORAGE_BUCKET para evitar a descoberta de buckets") from exc
            buckets = resposta.get("Buckets", [])
            if len(buckets) != 1 or resposta.get("ContinuationToken"):
                raise ValueError("Configure QD_STORAGE_BUCKET com o nome do bucket de destino")
            bucket = buckets[0]["Name"]
        return cls(client, endpoint, bucket)

    @property
    def destino(self):
        """Retorna os campos que identificam o destino confirmado no Neon."""
        return self.endpoint, self.bucket

    def enviar(self, path: Path, checksum: str) -> str:
        """Confere o arquivo local e reaproveita objetos idênticos em retentativas."""
        if not re.fullmatch(r"[0-9a-f]{64}", checksum or ""):
            raise ValueError("SHA-256 inválido para o PDF")
        key = f"querido-diario/pdf/{checksum[:2]}/{checksum}.pdf"
        with path.open("rb") as arquivo:
            if hashlib.file_digest(arquivo, "sha256").hexdigest() != checksum:
                raise ValueError("O PDF local não corresponde ao SHA-256 registrado")
            tamanho = arquivo.tell()
            arquivo.seek(0)
            try:
                remoto = self.client.head_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if status != 404:
                    raise
            else:
                if remoto["ContentLength"] != tamanho or remoto.get("Metadata", {}).get("sha256") != checksum:
                    raise ValueError("O objeto existente tem tamanho ou SHA-256 diferente do PDF local")
                return key
            self.client.put_object(
                Bucket=self.bucket, Key=key, Body=arquivo, ContentLength=tamanho,
                ContentType="application/pdf", Metadata={"sha256": checksum},
            )
        return key


def enviar_edicao(db, storage, raiz: Path, edicao_id: str) -> bool:
    """Confirma o upload após sucesso. Falhas deixam a edição disponível para reenvio."""
    try:
        with banco.exclusivo(db, "edicao:" + edicao_id):
            row = banco.pdf_para_envio(db, edicao_id)
            if not row or not row["pdf_disponivel"]:
                return False
            endpoint, bucket = storage.destino
            if (
                row["pdf_storage_endpoint"] == endpoint and row["pdf_storage_bucket"] == bucket
                and row["pdf_storage_key"] and row["pdf_storage_sha256"] == row["original_sha256"]
            ):
                return True
            if not row["original_caminho"]:
                raise ValueError("A edição não tem caminho local para o PDF")
            raiz = raiz.resolve()
            path = (raiz / row["original_caminho"]).resolve()
            if not path.is_relative_to(raiz):
                raise ValueError("Caminho do PDF fora do acervo")
            key = storage.enviar(path, row["original_sha256"])
            banco.confirmar_pdf(db, edicao_id, endpoint, bucket, key, row["original_sha256"])
            return True
    except (BotoCoreError, ClientError, OSError, ValueError, RuntimeError) as exc:
        codigo = exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError) else None
        LOG.error("PDF da edição %s não enviado ao bucket (%s).", edicao_id, codigo or type(exc).__name__)
        return False
