"""Regressões de integridade local e recuperação da coleta."""

import argparse
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from querido_diario.__main__ import _processar, sync
from querido_diario.coleta import _existente, coletar_edicao
from querido_diario.fonte import arquivar
from querido_diario.tipos import EdicaoFonte, ResumoColeta


class ColetaTest(unittest.TestCase):
    """Confere o cache e preserva edições válidas em lotes parcialmente inválidos."""

    def test_cache_confere_hash_mesmo_com_tamanho_igual(self) -> None:
        """Detecta corrupção de TXT e PDF mesmo sem alteração no tamanho."""
        with tempfile.TemporaryDirectory() as pasta:
            raiz = Path(pasta)
            for prefixo, bruto in (
                ("texto", b"texto original"),
                ("original", b"%PDF-1.7 original"),
            ):
                with self.subTest(prefixo=prefixo):
                    arquivo = arquivar(raiz, [bruto], texto=prefixo == "texto")
                    registro = {
                        prefixo + "_caminho": arquivo.caminho,
                        prefixo + "_sha256": arquivo.sha256,
                        "original_bytes": arquivo.tamanho,
                        "original_tipo": arquivo.tipo,
                    }
                    self.assertEqual(_existente(raiz, registro, prefixo), arquivo)
                    (raiz / arquivo.caminho).write_bytes(b"x" * len(bruto))
                    self.assertIsNone(_existente(raiz, registro, prefixo))

    def test_cache_rejeita_caminho_fora_do_acervo(self) -> None:
        """Bloqueia referências relativas e links simbólicos que escapam da raiz."""
        with tempfile.TemporaryDirectory() as pasta:
            externo = Path(pasta) / "fora.txt"
            externo.write_text("conteudo")
            raiz = Path(pasta) / "acervo"
            raiz.mkdir()
            (raiz / "link.txt").symlink_to(externo)
            for caminho in ("../fora.txt", "link.txt", str(externo)):
                with self.subTest(caminho=caminho), self.assertRaises(ValueError):
                    _existente(raiz, {"texto_caminho": caminho, "texto_sha256": "hash"}, "texto")

    def test_manifesto_invalido_nao_prepara_servicos(self) -> None:
        """Valida o manifesto antes da migração SQL e da criação do cliente S3."""
        with (
            tempfile.TemporaryDirectory() as pasta,
            patch("querido_diario.__main__.StoragePDF.do_ambiente") as storage,
            patch("querido_diario.banco.iniciar") as iniciar,
        ):
            manifesto = Path(pasta) / "manifesto.json"
            manifesto.write_text("[]")
            args = argparse.Namespace(manifesto=manifesto, inicio=None, fim=None)
            with self.assertRaises(ValueError):
                sync(Mock(), args)
            iniciar.assert_not_called()
            storage.assert_not_called()

    def test_edicao_invalida_nao_impede_proxima(self) -> None:
        """Registra uma falha para a entrada inválida e continua com a edição válida."""
        valida = {
            "territory_id": "3550308",
            "date": "2026-08-24",
            "url": "https://data.queridodiario.org.br/a",
        }
        resumo: ResumoColeta = {"edicoes": 0, "pdfs": 0, "textos": 0, "falhas": 0}
        with (
            patch("querido_diario.banco.exclusivo", return_value=nullcontext()),
            patch(
                "querido_diario.coleta.coletar_edicao",
                return_value={"pdf": False, "texto": True, "falhas": []},
            ) as coletar,
            self.assertLogs("querido_diario", level="ERROR"),
        ):
            _processar(Mock(), Mock(), Path("."), [None, valida], None, resumo, None)
        coletar.assert_called_once()
        self.assertEqual(resumo, {"edicoes": 1, "pdfs": 0, "textos": 1, "falhas": 1})

    def test_nova_coleta_reutiliza_arquivos_e_trechos(self) -> None:
        """Uma repetição com os mesmos metadados não baixa nem regrava a edição."""
        row: EdicaoFonte = {
            "territory_id": "3550308",
            "date": "2026-08-24",
            "url": "https://data.queridodiario.org.br/a",
            "txt_url": "https://data.queridodiario.org.br/a.txt",
        }
        with tempfile.TemporaryDirectory() as pasta:
            raiz = Path(pasta)
            original = arquivar(raiz, [b"%PDF-1.7 teste"], texto=False)
            texto = arquivar(raiz, [b"merenda escolar"], texto=True)
            fonte = Mock()
            fonte.baixar.side_effect = [original, texto]
            db = Mock()
            db.execute.return_value.fetchone.return_value = None
            with patch("querido_diario.banco.salvar") as salvar:
                coletar_edicao(db, fonte, raiz, row)
                registro = salvar.call_args.args[1]
                self.assertEqual(len(salvar.call_args.args[2]), 1)
                salvar.reset_mock()
                fonte.reset_mock()
                db.execute.return_value.fetchone.return_value = registro
                self.assertEqual(
                    coletar_edicao(db, fonte, raiz, row), {"pdf": True, "texto": True, "falhas": []}
                )
                salvar.assert_not_called()
                fonte.baixar.assert_not_called()
