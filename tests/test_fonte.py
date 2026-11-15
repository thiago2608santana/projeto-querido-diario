"""Regressões de HTTP, cobertura e validação de entradas, sem rede."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx

from querido_diario.fonte import (
    DEFAULT_API,
    MAX_MANIFEST,
    ErroListagem,
    Fonte,
    carregar_manifesto,
    validar_edicao,
)

DIA = date(2026, 8, 24)


class FonteTest(unittest.TestCase):
    """Distingue ausência de edições de falhas e limita entradas externas."""

    def test_cobertura_vazia_e_diferente_de_falha(self) -> None:
        """Uma resposta válida vazia mantém zero; uma falha mantém None e erros."""
        for status, esperado in ((200, 0), (403, None)):
            with self.subTest(status=status):
                resposta = httpx.Response(status, json={"gazettes": [], "total_gazettes": 0})
                with httpx.Client(transport=httpx.MockTransport(lambda _: resposta)) as client:
                    resultado = Fonte(client, DEFAULT_API, 0).cobertura("3550308", DIA, DIA)
                self.assertEqual(resultado["registros"], esperado)
                self.assertEqual(bool(resultado["erros"]), status != 200)

    def test_cobertura_preserva_ultima_data_se_periodo_falhar(self) -> None:
        """Mantém a informação válida de atualização mesmo com falha no período."""
        respostas = iter(
            [
                httpx.Response(
                    200,
                    json={
                        "gazettes": [{"territory_id": "3550308", "date": "2020-01-01"}],
                        "total_gazettes": 1,
                    },
                ),
                httpx.Response(403),
            ]
        )
        with httpx.Client(transport=httpx.MockTransport(lambda _: next(respostas))) as client:
            resultado = Fonte(client, DEFAULT_API, 0).cobertura("3550308", DIA, DIA)
        self.assertEqual(resultado["ultima_publicacao"], "2020-01-01")
        self.assertIsNone(resultado["registros"])
        self.assertEqual(resultado["erros"], ["período selecionado: HTTP 403"])

    def test_paginas_invalidas_geram_erro_controlado(self) -> None:
        """Rejeita contagens inválidas, registros não objetos e páginas incoerentes."""
        casos = [
            [],
            {"gazettes": [], "total_gazettes": True},
            {"gazettes": [None], "total_gazettes": 1},
            {"gazettes": [{}], "total_gazettes": 0},
            {"gazettes": [], "total_gazettes": 1},
        ]
        for corpo in casos:
            with self.subTest(corpo=corpo):
                with httpx.Client(
                    transport=httpx.MockTransport(lambda _: httpx.Response(200, json=corpo))
                ) as client:
                    fonte = Fonte(client, DEFAULT_API, 0)
                    with self.assertRaises(ErroListagem):
                        list(fonte.edicoes("3550308", DIA, DIA, 1))
                    self.assertTrue(fonte.cobertura("3550308", DIA, DIA)["erros"])

    def test_pagina_repetida_interrompe_paginacao(self) -> None:
        """Detecta a origem que ignora o offset antes de duplicar a mesma página."""
        corpo = {
            "total_gazettes": 5,
            "gazettes": [{"territory_id": "3550308", "date": DIA.isoformat()}],
        }
        with httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=corpo))
        ) as client:
            edicoes = Fonte(client, DEFAULT_API, 0).edicoes("3550308", DIA, DIA, 1)
            self.assertEqual(next(edicoes), corpo["gazettes"][0])
            with self.assertRaises(ErroListagem):
                next(edicoes)

    def test_resposta_excessiva_e_rejeitada(self) -> None:
        """Limita os bytes lidos mesmo quando o servidor não declara Content-Length."""
        with (
            patch("querido_diario.fonte.MAX_PAGE", 8),
            httpx.Client(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 9))
            ) as client,
        ):
            with self.assertRaises(ErroListagem):
                list(Fonte(client, DEFAULT_API, 0).edicoes("3550308", DIA, DIA, 1))

    def test_redirecionamento_valida_mesmo_com_cliente_configurado(self) -> None:
        """Desativa o redirecionamento automático para validar cada novo host."""
        chamadas: list[str] = []

        def responder(req: httpx.Request) -> httpx.Response:
            """Registra os destinos que efetivamente receberam uma requisição."""
            chamadas.append(str(req.url))
            return httpx.Response(302, headers={"location": "https://externo.example/arquivo"})

        with tempfile.TemporaryDirectory() as pasta:
            with httpx.Client(
                transport=httpx.MockTransport(responder), follow_redirects=True
            ) as client:
                with self.assertRaises(ValueError):
                    Fonte(client, DEFAULT_API, 0).baixar(
                        "https://data.queridodiario.org.br/a", Path(pasta)
                    )
        self.assertEqual(len(chamadas), 1)

    def test_retry_after_e_limite_de_tentativas(self) -> None:
        """Respeita a espera limitada e encerra depois de quatro respostas transitórias."""
        chamadas: list[httpx.Request] = []

        def responder(req: httpx.Request) -> httpx.Response:
            """Simula indisponibilidade contínua com Retry-After excessivo."""
            chamadas.append(req)
            return httpx.Response(503, headers={"Retry-After": "999"})

        with (
            patch("querido_diario.fonte.time.sleep") as esperar,
            self.assertLogs("querido_diario.fonte", level="WARNING"),
            httpx.Client(transport=httpx.MockTransport(responder)) as client,
        ):
            with self.assertRaises(ErroListagem):
                list(Fonte(client, DEFAULT_API, 0).edicoes("3550308", DIA, DIA, 1))
        self.assertEqual(len(chamadas), 4)
        self.assertEqual([c.args[0] for c in esperar.call_args_list if c.args[0]], [120, 120, 120])

    def test_ultima_data_representavel_nao_estoura(self) -> None:
        """Encerra a janela final antes de incrementar date.max."""
        with httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"gazettes": [], "total_gazettes": 0})
            )
        ) as client:
            self.assertEqual(
                list(Fonte(client, DEFAULT_API, 0).edicoes("3550308", date.max, date.max, 1)), []
            )

    def test_manifesto_valida_formato_e_tamanho(self) -> None:
        """Rejeita JSON malformado, raiz inválida e arquivo maior que o limite."""
        with tempfile.TemporaryDirectory() as pasta:
            path = Path(pasta) / "manifesto.json"
            for conteudo in (b"{", b"[]", b"{}", b'{"gazettes": null}', b" " * (MAX_MANIFEST + 1)):
                with self.subTest(tamanho=len(conteudo)):
                    path.write_bytes(conteudo)
                    with self.assertRaises(ValueError):
                        carregar_manifesto(path)
            path.write_text(json.dumps({"gazettes": [{"campo": "extra"}]}))
            self.assertEqual(carregar_manifesto(path), [{"campo": "extra"}])

    def test_edicao_valida_campos_preservando_metadados(self) -> None:
        """Aceita metadados extras e rejeita tipos inesperados nos campos consumidos."""
        valida = {
            "territory_id": "3550308",
            "date": "2026-08-24",
            "url": "https://data.queridodiario.org.br/a",
            "extra_metadata": [1],
        }
        self.assertIs(validar_edicao(valida), valida)
        for campos in (
            {"territory_id": None},
            {"date": "20260824"},
            {"url": None},
            {"scraped_at": "ontem"},
            {"edition": []},
            {"txt_url": 5},
            {"is_extra_edition": "false"},
        ):
            with self.subTest(campos=campos):
                with self.assertRaises(ValueError):
                    validar_edicao({**valida, **campos})
