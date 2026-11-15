"""Contratos compartilhados pela API, coleta e persistência.

``Registro`` representa linhas heterogêneas do Postgres ou metadados externos;
o ``Any`` fica nessa fronteira. Resultados controlados pelo pipeline têm tipos
específicos. As anotações não substituem a validação dos dados de entrada.
"""

from datetime import date
from typing import Any, NotRequired, TypedDict

from psycopg import Connection

type Registro = dict[str, Any]
type Conexao = Connection[Registro]
type Trecho = tuple[str, str, int, str]


class EdicaoFonte(TypedDict):
    """Campos consumidos da API; os campos adicionais são preservados em JSONB."""

    territory_id: str | int
    date: str
    url: str
    territory_name: NotRequired[str | None]
    state_code: NotRequired[str | None]
    edition: NotRequired[str | int | None]
    is_extra_edition: NotRequired[bool | None]
    file_checksum: NotRequired[str | None]
    txt_url: NotRequired[str | None]
    scraped_at: NotRequired[str | None]


class Recorte(TypedDict):
    """Filtros inclusivos; ``None`` significa ausência de filtro."""

    municipio: str | None
    inicio: date | None
    fim: date | None


class Cobertura(TypedDict):
    """Consulta da fonte; ``registros=None`` indica falha, não resultado vazio."""

    municipio_id: str
    ultima_publicacao: str | None
    registros: int | None
    erros: list[str]


class ResultadoColeta(TypedDict):
    """Disponibilidade local e nomes das etapas que falharam em uma edição."""

    pdf: bool
    texto: bool
    falhas: list[str]


class ResumoColeta(TypedDict):
    """Contadores da execução, com período e storage quando aplicáveis."""

    edicoes: int
    pdfs: int
    textos: int
    falhas: int
    inicio: NotRequired[date]
    fim: NotRequired[date]
    pdfs_no_storage: NotRequired[int]
    falhas_storage: NotRequired[int]


class ResumoEnvio(TypedDict):
    """Destino e contadores do envio independente de PDFs."""

    bucket: str
    pdfs_no_storage: int
    falhas: int


class ResumoCobertura(TypedDict):
    """Resultado agregado das consultas de atualização da fonte."""

    inicio: date
    fim: date
    cobertura: list[Cobertura]
    falhas: int
