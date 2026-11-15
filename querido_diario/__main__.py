"""Interface de linha de comando. A saída JSON serve também como entrada de agentes."""

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx
import psycopg
from dotenv import load_dotenv

from . import banco, coleta
from .fonte import DEFAULT_API, ErroListagem, Fonte, carregar_manifesto, validar_edicao
from .storage import StoragePDF, enviar_edicao, raiz_acervo
from .tipos import Conexao, Registro, ResumoCobertura, ResumoColeta, ResumoEnvio
from .vetores import BuscaVetorial, combinar, exigir

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter

LOG = logging.getLogger("querido_diario")


def municipio(value: str) -> str:
    """Valida o formato do código IBGE antes de consultar a API."""
    if len(value) != 7 or not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError("Informe um código IBGE de sete dígitos")
    return value


def inteiro(minimo: int, maximo: int) -> Callable[[str], int]:
    """Define limites no ponto de entrada para controlar uso de API e memória."""

    def validar(value: str) -> int:
        """Converte o argumento e rejeita valores fora dos limites inclusivos."""
        numero = int(value)
        if not minimo <= numero <= maximo:
            raise argparse.ArgumentTypeError(f"Valor deve estar entre {minimo} e {maximo}")
        return numero

    return validar


def argumentos() -> argparse.Namespace:
    """Expõe comandos independentes para ingestão, inspeção e recuperação."""
    parser = argparse.ArgumentParser(prog="python -m querido_diario", description=__doc__)
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("init", help="Criar esquema e tabelas no Neon")
    sync = sub.add_parser("sync", help="Coletar edições e preencher o índice textual")
    origem = sync.add_mutually_exclusive_group(required=True)
    origem.add_argument("--municipio", type=municipio, action="append")
    origem.add_argument(
        "--manifesto", type=Path, help="Ler uma resposta JSON de /gazettes salva em disco"
    )
    sync.add_argument("--inicio", type=date.fromisoformat)
    sync.add_argument("--fim", type=date.fromisoformat)
    sync.add_argument("--dias", type=inteiro(1, 366), default=7)
    sync.add_argument("--pagina", type=inteiro(1, 50), default=25)
    sync.add_argument(
        "--docling", action="store_true", help="Extrair texto quando o TXT não está disponível"
    )
    sync.add_argument(
        "--sem-storage", action="store_true", help="Coletar sem copiar PDFs ao bucket"
    )
    cobertura = sub.add_parser("cobertura", help="Consultar atualização e registros na fonte")
    cobertura.add_argument("--municipio", type=municipio, action="append", required=True)
    cobertura.add_argument("--inicio", type=date.fromisoformat, required=True)
    cobertura.add_argument("--fim", type=date.fromisoformat, required=True)
    envio = sub.add_parser("enviar-pdfs", help="Copiar PDFs pendentes do acervo local ao bucket")
    envio.add_argument("--municipio", type=municipio)
    envio.add_argument("--inicio", type=date.fromisoformat)
    envio.add_argument("--fim", type=date.fromisoformat)
    envio.add_argument("--maximo", type=inteiro(1, 100000), default=500)
    index = sub.add_parser("indexar", help="Enviar ao Pinecone os trechos ainda pendentes")
    index.add_argument("--maximo", type=inteiro(1, 100000), default=500)
    busca = sub.add_parser("buscar", help="Retornar trechos, pontuação e citações em JSON")
    busca.add_argument("consulta")
    busca.add_argument("--modo", choices=["lexical", "semantica", "hibrida"], default="lexical")
    busca.add_argument("--municipio", type=municipio)
    busca.add_argument("--inicio", type=date.fromisoformat)
    busca.add_argument("--fim", type=date.fromisoformat)
    busca.add_argument("--limite", type=inteiro(1, 100), default=10)
    show = sub.add_parser("edicao", help="Consultar os metadados completos de uma edição no Neon")
    show.add_argument("id")
    return parser.parse_args()


def intervalo_api() -> float:
    """Respeita o mesmo limite de taxa na consulta de cobertura e na coleta."""
    taxa = float(os.getenv("QD_REQUISICOES_POR_MINUTO", "45"))
    if not 1 <= taxa <= 60:
        raise ValueError("QD_REQUISICOES_POR_MINUTO deve estar entre 1 e 60")
    return 60 / taxa


def cobertura(args: argparse.Namespace) -> ResumoCobertura:
    """Consulta exclusivamente a fonte pública, sem depender do Neon."""
    if args.inicio > args.fim:
        raise ValueError("A data inicial deve ser igual ou anterior à final")
    with httpx.Client(timeout=httpx.Timeout(15, connect=8)) as http:
        fonte = Fonte(http, os.getenv("QD_API_URL", DEFAULT_API), intervalo_api())
        resultados = [
            fonte.cobertura(codigo, args.inicio, args.fim)
            for codigo in dict.fromkeys(args.municipio)
        ]
    return {
        "inicio": args.inicio,
        "fim": args.fim,
        "cobertura": resultados,
        "falhas": sum(bool(row["erros"]) for row in resultados),
    }


def sync(db: Conexao, args: argparse.Namespace) -> ResumoColeta:
    """Sincroniza recortes, preservando edições já gravadas em caso de interrupção."""
    manifesto: list[object] | None = None
    if args.manifesto:
        if args.inicio or args.fim:
            raise ValueError("Use o manifesto com o recorte já selecionado")
        manifesto = carregar_manifesto(args.manifesto)
    fim = args.fim or datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    inicio = args.inicio or fim - timedelta(days=args.dias - 1)
    if inicio > fim:
        raise ValueError("A data inicial deve ser igual ou anterior à final")
    raiz = raiz_acervo()
    storage = None if args.sem_storage else StoragePDF.do_ambiente()
    hosts = os.getenv("QD_HOSTS_ARQUIVOS", "").split()
    intervalo = intervalo_api()
    converter = None
    if args.docling:
        from docling.document_converter import DocumentConverter

        converter = DocumentConverter()
    resumo: ResumoColeta = {"edicoes": 0, "pdfs": 0, "textos": 0, "falhas": 0}
    if storage:
        resumo.update({"pdfs_no_storage": 0, "falhas_storage": 0})
    if not args.manifesto:
        resumo.update({"inicio": inicio, "fim": fim})
    banco.iniciar(db)
    with httpx.Client(timeout=httpx.Timeout(45, connect=10), follow_redirects=False) as http:
        fonte = Fonte(http, os.getenv("QD_API_URL", DEFAULT_API), intervalo, hosts)
        if manifesto is not None:
            _processar(db, fonte, raiz, manifesto, converter, resumo, storage)
        else:
            for codigo in dict.fromkeys(args.municipio):
                try:
                    LOG.info("Consultando município %s de %s a %s.", codigo, inicio, fim)
                    rows = fonte.edicoes(codigo, inicio, fim, args.pagina)
                    _processar(db, fonte, raiz, rows, converter, resumo, storage)
                except ErroListagem as exc:
                    LOG.error("Não foi possível listar as edições de %s.", exc)
                    resumo["falhas"] += 1
                except (httpx.HTTPError, KeyError, ValueError) as exc:
                    LOG.error(
                        "Não foi possível listar as edições de %s (%s).", codigo, type(exc).__name__
                    )
                    resumo["falhas"] += 1
    return resumo


def _processar(
    db: Conexao,
    fonte: Fonte,
    raiz: Path,
    rows: Iterable[object],
    converter: "DocumentConverter | None",
    resumo: ResumoColeta,
    storage: StoragePDF | None,
) -> None:
    """Isola falhas por edição e soma apenas etapas concluídas no resumo."""
    for valor in rows:
        try:
            row = validar_edicao(valor)
            chave = coleta.chave_edicao(row)
            with banco.exclusivo(db, "edicao:" + chave):
                result = coleta.coletar_edicao(db, fonte, raiz, row, converter)
            resumo["edicoes"] += 1
            resumo["pdfs"] += result["pdf"]
            resumo["textos"] += result["texto"]
            falha_storage = False
            if storage and result["pdf"]:
                enviado = enviar_edicao(db, storage, raiz, chave)
                resumo["pdfs_no_storage"] += enviado
                falha_storage = not enviado
                resumo["falhas_storage"] += falha_storage
            resumo["falhas"] += bool(result["falhas"]) or falha_storage
            LOG.info(
                "Edição de %s processada (%s). Total: %s.",
                row["territory_id"],
                row["date"],
                resumo["edicoes"],
            )
        except (KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
            LOG.error("Uma edição não pôde ser processada (%s).", type(exc).__name__)
            resumo["falhas"] += 1


def enviar_pdfs(db: Conexao, args: argparse.Namespace) -> ResumoEnvio:
    """Envia PDFs já coletados, sem depender da API de listagem do Querido Diário."""
    if args.inicio and args.fim and args.inicio > args.fim:
        raise ValueError("A data inicial deve ser igual ou anterior à final")
    storage = StoragePDF.do_ambiente(obrigatorio=True)
    if storage is None:
        raise ValueError("Configure o Object Storage para enviar PDFs")
    banco.iniciar(db)
    recorte = banco.filtros(args.municipio, args.inicio, args.fim)
    raiz = raiz_acervo()
    resumo: ResumoEnvio = {"bucket": storage.bucket, "pdfs_no_storage": 0, "falhas": 0}
    cursor = ""
    for _ in range(0, args.maximo, 100):
        processados = resumo["pdfs_no_storage"] + resumo["falhas"]
        lote = banco.pdfs_pendentes(
            db, *storage.destino, recorte, cursor, min(100, args.maximo - processados)
        )
        if not lote:
            break
        for row in lote:
            cursor = row["id"]
            enviado = enviar_edicao(db, storage, raiz, cursor)
            resumo["pdfs_no_storage"] += enviado
            resumo["falhas"] += not enviado
            LOG.info(
                "Envio de PDFs: %s confirmados, %s falhas.",
                resumo["pdfs_no_storage"],
                resumo["falhas"],
            )
    return resumo


def buscar(db: Conexao, args: argparse.Namespace) -> list[Registro]:
    """Executa busca por termos, similaridade ou fusão de rankings."""
    if not args.consulta.strip() or len(args.consulta) > 2000:
        raise ValueError("Informe uma consulta com até 2000 caracteres")
    if args.inicio and args.fim and args.inicio > args.fim:
        raise ValueError("A data inicial deve ser igual ou anterior à final")
    recorte = banco.filtros(args.municipio, args.inicio, args.fim)
    if args.modo == "lexical":
        return banco.lexical(db, args.consulta, recorte, args.limite)
    vetorial = BuscaVetorial()
    if args.modo == "semantica":
        return vetorial.buscar(db, args.consulta, recorte, args.limite)
    candidatos = min(args.limite * 3, 100)
    return combinar(
        banco.lexical(db, args.consulta, recorte, candidatos),
        vetorial.buscar(db, args.consulta, recorte, candidatos),
        args.limite,
    )


def main() -> int:
    """Mantém resultados no stdout e erros no stderr, sem expor o DSN."""
    args = argumentos()
    load_dotenv(Path.cwd() / ".env", override=False)
    nivel = logging.INFO if os.getenv("QD_LOG_LEVEL") == "INFO" else logging.WARNING
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    LOG.setLevel(nivel)
    resultado: object
    try:
        if args.comando == "cobertura":
            resultado = cobertura(args)
        else:
            with banco.conectar(exigir("DATABASE_URL")) as db:
                if args.comando == "init":
                    banco.iniciar(db)
                    resultado = {"schema": "querido_diario"}
                elif args.comando == "sync":
                    resultado = sync(db, args)
                elif args.comando == "enviar-pdfs":
                    resultado = enviar_pdfs(db, args)
                elif args.comando == "indexar":
                    resultado = {"indexados": BuscaVetorial().indexar(db, args.maximo)}
                elif args.comando == "buscar":
                    resultado = buscar(db, args)
                else:
                    resultado = db.execute(
                        "SELECT * FROM querido_diario.edicoes WHERE id = %s", (args.id,)
                    ).fetchone()
        print(json.dumps(resultado, default=str, ensure_ascii=False, indent=2))
        falhas = resultado.get("falhas", 0) if isinstance(resultado, dict) else 0
        return int(bool(falhas) or resultado is None)
    except psycopg.Error as exc:
        LOG.error(
            "Falha no Postgres (%s). Confira DATABASE_URL, permissões e se o esquema foi criado.",
            exc.sqlstate or type(exc).__name__,
        )
        return 1
    except (ValueError, RuntimeError) as exc:
        LOG.error("%s", exc)
        return 1
    except Exception as exc:
        LOG.error(
            "Falha ao executar %s (%s). Confira a configuração dos serviços.",
            args.comando,
            type(exc).__name__,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
