"""Valida contratos de coleta e busca sem credenciais externas."""

import tempfile
import unittest
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from querido_diario.coleta import dividir
from querido_diario.fonte import Fonte, arquivar, carregar_texto
from querido_diario.vetores import BuscaVetorial, combinar


class PipelineTest(unittest.TestCase):
    """Protege paginação, deduplicação e fusão de busca contra regressões."""

    def test_paginacao_sem_palavra_chave(self) -> None:
        """Percorre offsets da mesma data sem restringir a consulta por assunto."""

        def responder(req: httpx.Request) -> httpx.Response:
            """Simula a origem e verifica os parâmetros enviados pelo cliente."""
            p = req.url.params
            self.assertNotIn("querystring", p)
            self.assertEqual(p["published_since"], p["published_until"])
            self.assertEqual(p["territory_ids"], "3509502")
            return httpx.Response(
                200,
                json={
                    "total_gazettes": 2,
                    "gazettes": [
                        {
                            "territory_id": "3509502",
                            "date": p["published_since"],
                            "edition": p["offset"],
                        }
                    ],
                },
            )

        with httpx.Client(transport=httpx.MockTransport(responder)) as client:
            fonte = Fonte(client, "https://api.queridodiario.org.br", 0)
            dia = date(2026, 8, 24)
            rows = list(fonte.edicoes("3509502", dia, dia, 1))
        self.assertEqual([r["edition"] for r in rows], ["0", "1"])

    def test_arquivo_bruto_e_deduplicacao(self) -> None:
        """Preserva os bytes, deduplica arquivos e rejeita páginas HTML."""
        with tempfile.TemporaryDirectory() as path:
            raiz = Path(path)
            bruto = b"%PDF-1.7\nconteudo"
            primeiro = arquivar(raiz, [bruto], texto=False)
            segundo = arquivar(raiz, [bruto[:8], bruto[8:]], texto=False)
            self.assertEqual(primeiro, segundo)
            self.assertTrue(primeiro.caminho.endswith(".pdf"))
            self.assertEqual((raiz / primeiro.caminho).read_bytes(), bruto)
            txt = arquivar(raiz, ["alimentação".encode()], texto=True)
            self.assertEqual(carregar_texto(raiz / txt.caminho), "alimentação")
            with self.assertRaises(ValueError):
                arquivar(raiz, [b"<html>bad gateway</html>"], texto=False)

    def test_redirecionamento_externo_nao_e_seguido(self) -> None:
        """Bloqueia o destino externo antes que uma segunda requisição seja enviada."""

        def responder(req: httpx.Request) -> httpx.Response:
            """Simula a origem e verifica os parâmetros enviados pelo cliente."""
            self.assertEqual(req.url.host, "data.queridodiario.org.br")
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

        with tempfile.TemporaryDirectory() as path:
            with httpx.Client(transport=httpx.MockTransport(responder)) as client:
                fonte = Fonte(client, "https://api.queridodiario.org.br", 0)
                with self.assertRaises(ValueError):
                    fonte.baixar("https://data.queridodiario.org.br/exemplo", Path(path))

    def test_busca_hibrida_prioriza_resultado_comum(self) -> None:
        """Soma posições de ambos os rankings para ordenar resultados compartilhados."""
        a, b, c = ({"trecho_id": key, "score": 50} for key in "abc")
        hits = combinar([a, b], [c, b], 3)
        self.assertEqual(hits[0]["trecho_id"], "b")
        self.assertEqual(hits[0]["fontes"], ["lexical", "semantica"])

    def test_trechos_limitados(self) -> None:
        """Mantém tamanho e sobreposição dentro do contrato de segmentação."""
        chunks = list(dividir(" ".join(f"palavra{i}" for i in range(1000))))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 1800 for c in chunks))
        self.assertIn(" ".join(chunks[0].split()[-5:]), chunks[1][:240])

    def test_lote_nao_confirmado_pode_ser_reenviado_com_o_mesmo_id(self) -> None:
        """Confirma somente lotes completos e repete os mesmos IDs após falha."""
        busca = BuscaVetorial.__new__(BuscaVetorial)
        busca.perfil, busca.namespace = "perfil-teste", "namespace-teste"
        busca.index = Mock()
        busca.embeddings = Mock(return_value=[[0.1, 0.2]])
        lote = [
            {
                "id": "trecho1",
                "texto": "merenda escolar",
                "municipio_id": "3509502",
                "publicada_em": date(2026, 8, 24),
            }
        ]
        busca.index.upsert.side_effect = [
            SimpleNamespace(upserted_count=0),
            SimpleNamespace(upserted_count=1),
        ]
        with (
            patch("querido_diario.banco.exclusivo", return_value=nullcontext()),
            patch("querido_diario.banco.pendentes", return_value=lote),
            patch("querido_diario.banco.confirmar") as confirmar,
        ):
            with self.assertRaises(RuntimeError):
                busca.indexar(None, 1)
            confirmar.assert_not_called()
            self.assertEqual(busca.indexar(None, 1), 1)
            confirmar.assert_called_once_with(None, "perfil-teste", ["trecho1"])
        primeiro, segundo = busca.index.upsert.call_args_list
        self.assertEqual(primeiro, segundo)
