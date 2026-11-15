"""Regressões do controlador local e dos filtros usados pelo painel."""

import unittest
from datetime import date
from unittest.mock import Mock, patch

from app import Execucao, codigos, datas
from querido_diario.capitais import CAPITAIS, rotulo_municipio


class PainelTest(unittest.TestCase):
    """Valida o painel sem iniciar subprocessos ou acessar serviços."""

    def test_capitais_e_codigos_manuais_sem_duplicatas(self) -> None:
        """Combina os códigos selecionados preservando a ordem e o formato IBGE."""
        self.assertEqual(len(CAPITAIS), 27)
        self.assertEqual(
            codigos("3550308 3106200,3550308\n3509502", True), ["3550308", "3106200", "3509502"]
        )
        self.assertEqual(rotulo_municipio("3550308"), "São Paulo (SP) · 3550308")
        for valor in ("", "abc", "355030", "３５５０３０８"):
            with self.subTest(valor=valor), self.assertRaises(ValueError):
                codigos(valor, True)

    def test_datas_opcionais_e_invertidas(self) -> None:
        """Transforma apenas datas preenchidas e rejeita intervalos invertidos."""
        dia = date(2026, 8, 24)
        self.assertEqual(datas(None, dia), ["--fim", "2026-08-24"])
        self.assertEqual(datas(None, None), [])
        with self.assertRaises(ValueError):
            datas(dia, date(2026, 8, 23))

    def test_controlador_nao_aceita_duas_execucoes(self) -> None:
        """Rejeita uma nova execução enquanto o subprocesso anterior está ativo."""
        painel = Execucao()
        self.addCleanup(painel.pasta.cleanup)
        with patch("app.subprocess.Popen") as iniciar:
            iniciar.return_value.poll.return_value = None
            painel.iniciar("Coleta", ["sync", "--municipio", "3550308"])
            with self.assertRaises(ValueError):
                painel.iniciar("Outra", ["sync"])
            iniciar.assert_called_once()
            painel.cancelar()
            iniciar.return_value.terminate.assert_called_once()
            self.assertTrue(painel.cancelada)

    def test_resultado_invalido_nao_quebra_painel(self) -> None:
        """Trata JSON escalar ou malformado como ausência de resumo, mantendo o log."""
        painel = Execucao()
        self.addCleanup(painel.pasta.cleanup)
        painel.processo = Mock()
        painel.processo.poll.return_value = 1
        (painel.raiz / "log").write_text("falha controlada")
        for bruto in ("null", "[]", '"texto"', "{incompleto"):
            with self.subTest(bruto=bruto):
                (painel.raiz / "resultado").write_text(bruto)
                estado = painel.estado()
                self.assertIsNotNone(estado)
                assert estado is not None
                self.assertEqual(estado[3], {})
                self.assertEqual(estado[4], "falha controlada")
