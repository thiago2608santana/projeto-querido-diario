"""Testa o reenvio de PDFs e a confirmação no Postgres sem serviços externos."""

import hashlib
import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from querido_diario.storage import StoragePDF, enviar_edicao


class StorageTest(unittest.TestCase):
    """Verifica integridade e confirmação de uploads sem acessar o bucket."""

    def test_tamanho_independe_da_posicao_apos_hash(self) -> None:
        """Obtém o tamanho pelo descritor mesmo se o cálculo do hash reposicionar a leitura."""
        bruto = b"%PDF-1.7\nPDF de teste"
        checksum = hashlib.sha256(bruto).hexdigest()
        client = Mock()
        client.head_object.side_effect = ClientError(
            {"ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject"
        )

        def digest(arquivo: Any, algoritmo: str) -> Any:
            """Simula um cálculo de hash que deixa o descritor no início do arquivo."""
            resultado = hashlib.new(algoritmo, arquivo.read())
            arquivo.seek(0)
            return resultado

        with TemporaryDirectory() as folder, patch("hashlib.file_digest", side_effect=digest):
            path = Path(folder) / "diario.pdf"
            path.write_bytes(bruto)
            StoragePDF(client, "https://example.com", "diarios").enviar(path, checksum)
        self.assertEqual(client.put_object.call_args.kwargs["ContentLength"], len(bruto))

    def test_envia_pdf_e_reaproveita_objeto_pelo_checksum(self) -> None:
        """Envia os bytes corretos e reaproveita a confirmação remota compatível."""
        bruto = b"%PDF-1.7\nPDF de teste"
        checksum = hashlib.sha256(bruto).hexdigest()
        client = Mock()
        client.head_object.side_effect = [
            ClientError({"ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject"),
            {"ContentLength": len(bruto), "Metadata": {"sha256": checksum}},
        ]

        def put(**params: Any) -> None:
            """Inspeciona o corpo e os metadados que seriam enviados ao S3."""
            self.assertEqual(params["Body"].read(), bruto)
            self.assertEqual(params["ContentLength"], len(bruto))
            self.assertEqual(params["ContentType"], "application/pdf")

        client.put_object.side_effect = put
        storage = StoragePDF(client, "https://example.com", "diarios")
        with TemporaryDirectory() as folder:
            path = Path(folder) / "diario.pdf"
            path.write_bytes(bruto)
            key = storage.enviar(path, checksum)
            self.assertEqual(storage.enviar(path, checksum), key)
        self.assertEqual(key, f"querido-diario/pdf/{checksum[:2]}/{checksum}.pdf")
        client.put_object.assert_called_once()

    def test_falha_na_copia_nao_confirma_referencia(self) -> None:
        """Deixa o PDF pendente quando o upload falha e confirma na repetição."""
        storage = Mock(destino=("https://example.com", "diarios"))
        storage.enviar.side_effect = TimeoutError()
        row = {
            "pdf_disponivel": True,
            "original_caminho": "diario.pdf",
            "original_sha256": "checksum",
            "pdf_storage_endpoint": None,
            "pdf_storage_bucket": None,
            "pdf_storage_key": None,
            "pdf_storage_sha256": None,
        }
        with (
            TemporaryDirectory() as folder,
            patch("querido_diario.banco.exclusivo", return_value=nullcontext()),
            patch("querido_diario.banco.pdf_para_envio", return_value=row),
            patch("querido_diario.banco.confirmar_pdf") as confirmar,
            self.assertLogs("querido_diario.storage", level="ERROR"),
        ):
            self.assertFalse(enviar_edicao(None, storage, Path(folder), "edicao1"))
            confirmar.assert_not_called()
            storage.enviar.side_effect = None
            storage.enviar.return_value = "diario.pdf"
            self.assertTrue(enviar_edicao(None, storage, Path(folder), "edicao1"))
            confirmar.assert_called_once_with(
                None,
                "edicao1",
                "https://example.com",
                "diarios",
                "diario.pdf",
                "checksum",
            )
