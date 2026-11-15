"""Exercita o resumo de edições sem chamadas externas."""

import json
import unittest
from unittest.mock import Mock

from openai import OpenAI

from tools.resumir_diario import resumir_diario


class ResumirDiarioTest(unittest.TestCase):
    """Verifica limpeza, consolidação e integridade das evidências."""

    def test_limpa_rodapes_e_preserva_origem_na_consolidacao(self) -> None:
        """Combina dois blocos e mantém a evidência extraída do segundo."""
        a = "Foi aberto o pregão municipal."
        b = "Foi publicada uma retificação."
        texto = "\n".join([a, "Página 1 de 3", "Página 1 de 3", "Página 1 de 3", "á" * 520, b])
        cliente = Mock(spec=OpenAI)
        cliente.responses.create.side_effect = [
            Mock(
                status="completed",
                output_text=json.dumps(
                    {"pontos": [{"tema": "Licitação", "descricao": a, "evidencia": a}]}
                ),
            ),
            Mock(
                status="completed",
                output_text=json.dumps(
                    {"pontos": [{"tema": "Retificação", "descricao": b, "evidencia": b}]}
                ),
            ),
            Mock(status="completed", output_text='{"ids":[2,1]}'),
        ]
        resumo = resumir_diario(texto, cliente=cliente, bytes_por_bloco=1024)
        self.assertEqual(resumo.linhas_removidas, 2)
        self.assertEqual(resumo.blocos, 2)
        self.assertEqual([(p.bloco, p.evidencia) for p in resumo.pontos], [(2, b), (1, a)])
        for chamada in cliente.responses.create.call_args_list:
            self.assertFalse(chamada.kwargs["store"])
            formato = chamada.kwargs["text"]["format"]
            self.assertTrue(formato["strict"])
            self.assertFalse(formato["schema"]["additionalProperties"])

    def test_rejeita_evidencia_inventada_e_resposta_incompleta(self) -> None:
        """Sinaliza falhas sem apresentar um resumo parcial como sucesso."""
        cliente = Mock(spec=OpenAI)
        cliente.responses.create.return_value = Mock(
            status="completed",
            output_text='{"pontos":[{"tema":"Pregão","descricao":"Aberto","evidencia":"Inventada"}]}',
        )
        with self.assertRaisesRegex(RuntimeError, "não consta"):
            resumir_diario("Texto da edição", cliente=cliente)
        cliente.responses.create.return_value.status = "incomplete"
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            resumir_diario("Texto da edição", cliente=cliente)

    def test_limite_antes_de_chamar_api(self) -> None:
        """Rejeita uma edição acima do orçamento configurado de blocos."""
        cliente = Mock(spec=OpenAI)
        with self.assertRaisesRegex(ValueError, "acima do limite"):
            resumir_diario("á" * 2000, cliente=cliente, bytes_por_bloco=1024, max_blocos=1)
        cliente.responses.create.assert_not_called()
