"""Contratos de embeddings sem chamadas a provedores externos."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from querido_diario.vetores import BuscaVetorial


class VetoresTest(unittest.TestCase):
    """Impede que respostas inválidas associem vetores aos trechos errados."""

    def criar_busca(self) -> BuscaVetorial:
        """Cria apenas o cliente simulado necessário para testar embeddings."""
        busca = BuscaVetorial.__new__(BuscaVetorial)
        busca.provider, busca.model, busca.dim = "openai", "teste", 2
        busca.embed_client = Mock()
        return busca

    def test_indices_fora_de_ordem_sao_reordenados(self) -> None:
        """Associa cada vetor ao texto de origem usando o índice do provedor."""
        busca = self.criar_busca()
        busca.embed_client.embeddings.create.return_value.data = [
            SimpleNamespace(index=1, embedding=[0.3, 0.4]),
            SimpleNamespace(index=0, embedding=[0.1, 0.2]),
        ]
        self.assertEqual(busca.embeddings(["um", "dois"]), [[0.1, 0.2], [0.3, 0.4]])

    def test_indices_duplicados_sao_rejeitados(self) -> None:
        """Não aceita dois vetores vinculados ao mesmo índice de entrada."""
        busca = self.criar_busca()
        busca.embed_client.embeddings.create.return_value.data = [
            SimpleNamespace(index=0, embedding=[0.1, 0.2]),
            SimpleNamespace(index=0, embedding=[0.3, 0.4]),
        ]
        with self.assertRaises(ValueError):
            busca.embeddings(["um", "dois"])

    def test_dimensao_e_valores_nao_finitos_sao_rejeitados(self) -> None:
        """Recusa dimensão errada, NaN e infinito antes do envio ao índice."""
        busca = self.criar_busca()
        for vetor in ([0.1], [0.1, float("nan")], [float("inf"), 0.2]):
            with self.subTest(vetor=vetor), self.assertRaises(ValueError):
                busca.embed_client.embeddings.create.return_value.data = [
                    SimpleNamespace(index=0, embedding=vetor)
                ]
                busca.embeddings(["texto"])

    def test_lista_vazia_nao_chama_provedor(self) -> None:
        """Retorna uma lista vazia sem requisição cobrável para nenhum texto."""
        busca = self.criar_busca()
        self.assertEqual(busca.embeddings([]), [])
        busca.embed_client.embeddings.create.assert_not_called()
