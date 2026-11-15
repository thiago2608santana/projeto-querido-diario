"""Mantém docstrings e assinaturas anotadas sem depender de ferramentas extras."""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PadronizacaoTest(unittest.TestCase):
    """Verifica contratos de documentação e tipagem em fontes e testes."""

    def test_funcoes_classes_e_modulos_documentados(self) -> None:
        """Exige docstrings e tipos em todas as funções, inclusive privadas e internas."""
        arquivos = [
            ROOT / "app.py",
            *sorted((ROOT / "querido_diario").glob("*.py")),
            *sorted((ROOT / "tests").glob("*.py")),
        ]
        for arquivo in arquivos:
            arvore = ast.parse(arquivo.read_text(encoding="utf-8"))
            with self.subTest(arquivo=arquivo.name):
                self.assertTrue(ast.get_docstring(arvore), "Módulo sem docstring")
            for no in ast.walk(arvore):
                if isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    with self.subTest(arquivo=arquivo.name, nome=no.name, linha=no.lineno):
                        self.assertTrue(ast.get_docstring(no), "Falta docstring")
                        if isinstance(no, ast.ClassDef):
                            continue
                        self.assertIsNotNone(no.returns, "Falta anotação de retorno")
                        argumentos = [*no.args.posonlyargs, *no.args.args, *no.args.kwonlyargs]
                        argumentos += [arg for arg in (no.args.vararg, no.args.kwarg) if arg]
                        for arg in argumentos:
                            if arg.arg not in ("self", "cls"):
                                self.assertIsNotNone(arg.annotation, f"Falta tipo de {arg.arg}")
