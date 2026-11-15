"""Cliente HTTP com paginação, retentativas e armazenamento por SHA-256."""

import hashlib
import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import urljoin, urlsplit

import httpx

from .tipos import Cobertura, EdicaoFonte, Registro

LOG = logging.getLogger(__name__)
RETRY_CODES = {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
DEFAULT_API = "https://api.queridodiario.org.br"
FILE_HOSTS = {
    "data.queridodiario.org.br",
    "data.queridodiario.ok.org.br",
    "querido-diario.nyc3.digitaloceanspaces.com",
    "querido-diario.nyc3.cdn.digitaloceanspaces.com",
}
MAX_FILE = 150 * 1024 * 1024
MAX_TEXT = 25 * 1024 * 1024
MAX_PAGE = 10 * 1024 * 1024
MAX_MANIFEST = 5 * 1024 * 1024


@dataclass(frozen=True)
class Arquivo:
    """Referência verificável a um arquivo dentro da pasta do acervo."""

    sha256: str
    caminho: str
    tipo: str
    tamanho: int


def sha256(data: bytes) -> str:
    """Calcula um identificador estável para os bytes recebidos."""
    return hashlib.sha256(data).hexdigest()


def _validate_url(url: str, hosts: set[str]) -> None:
    """Rejeita URLs com esquema, host, porta ou credenciais não autorizados."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("URL fora dos hosts HTTPS autorizados para o Querido Diário")


def motivo_http(exc: httpx.HTTPError) -> str:
    """Resume o erro sem expor URL, parâmetros ou conteúdo da resposta."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def validar_edicao(valor: object) -> EdicaoFonte:
    """Valida campos consumidos antes de baixar arquivos ou persistir uma edição.

    Args:
        valor: Registro recebido da API ou de um manifesto.

    Returns:
        O mesmo registro validado, preservando campos desconhecidos para o JSONB.

    Raises:
        ValueError: Um campo obrigatório está ausente ou tem formato inválido.
            A mensagem não inclui o conteúdo do documento.
    """
    if not isinstance(valor, dict):
        raise ValueError("A edição deve ser um objeto JSON")
    codigo = str(valor.get("territory_id", ""))
    if len(codigo) != 7 or not codigo.isascii() or not codigo.isdecimal():
        raise ValueError("Código IBGE inválido na edição")
    if not isinstance(valor.get("date"), str):
        raise ValueError("Data inválida na edição")
    try:
        publicada = date.fromisoformat(valor["date"])
    except ValueError as exc:
        raise ValueError("Data inválida na edição") from exc
    if publicada.isoformat() != valor["date"]:
        raise ValueError("A data da edição deve usar YYYY-MM-DD")
    if not isinstance(valor.get("url"), str) or not valor["url"].strip():
        raise ValueError("URL do original ausente ou inválida")
    for campo in ("territory_name", "state_code", "file_checksum", "txt_url", "scraped_at"):
        if valor.get(campo) is not None and not isinstance(valor[campo], str):
            raise ValueError(f"Campo {campo} inválido na edição")
    if valor.get("is_extra_edition") is not None and type(valor["is_extra_edition"]) is not bool:
        raise ValueError("Indicador de edição extra inválido")
    if valor.get("edition") is not None and type(valor["edition"]) not in (str, int):
        raise ValueError("Número da edição inválido")
    if valor.get("scraped_at"):
        try:
            datetime.fromisoformat(valor["scraped_at"])
        except ValueError as exc:
            raise ValueError("Data de coleta da fonte inválida") from exc
    return cast(EdicaoFonte, valor)


def carregar_manifesto(path: Path) -> list[object]:
    """Lê até 5 MiB de JSON; cada edição é validada separadamente na coleta.

    Rejeita a estrutura inválida antes de preparar o banco ou o storage. A leitura
    limitada protege também a CLI; o tamanho declarado na interface não é usado
    como garantia de tamanho do arquivo em disco.
    """
    with path.open("rb") as arquivo:
        bruto = arquivo.read(MAX_MANIFEST + 1)
    if len(bruto) > MAX_MANIFEST:
        raise ValueError("O manifesto deve ter até 5 MB")
    try:
        dados = json.loads(bruto)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("O manifesto deve conter JSON válido") from exc
    if not isinstance(dados, dict) or not isinstance(dados.get("gazettes"), list):
        raise ValueError("O manifesto deve conter uma lista em gazettes")
    return cast(list[object], dados["gazettes"])


class ErroListagem(RuntimeError):
    """Identifica a página que falhou após as tentativas da API."""


class Fonte:
    """Compartilha uma conexão e limita a taxa inclusive nas retentativas."""

    def __init__(
        self,
        client: httpx.Client,
        base: str,
        interval: float,
        extra_hosts: Iterable[str] = (),
    ) -> None:
        """Valida a origem HTTPS e recebe um cliente cuja vida útil é do chamador."""
        host = urlsplit(base).hostname
        if not host or not math.isfinite(interval) or interval < 0:
            raise ValueError("Origem da API ou intervalo entre requisições inválido")
        _validate_url(base, {host})
        if urlsplit(base).query or urlsplit(base).fragment:
            raise ValueError("A origem da API não deve conter parâmetros ou fragmentos")
        self.client = client
        self.base = base.rstrip("/")
        self.interval = interval
        self.next_request = 0.0
        self.api_hosts = {host, "api.queridodiario.ok.org.br", "api.queridodiario.org.br"}
        self.file_hosts = FILE_HOSTS | set(extra_hosts)

    def _send(self, url: str, hosts: set[str]) -> httpx.Response:
        """Aplica o limite de taxa e valida cada destino antes de redirecionar."""
        for _ in range(6):
            _validate_url(url, hosts)
            time.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + self.interval
            response = self.client.send(
                self.client.build_request("GET", url),
                stream=True,
                follow_redirects=False,
            )
            if not response.is_redirect or "location" not in response.headers:
                return response
            url = urljoin(url, response.headers["location"])
            response.close()
        raise ValueError("Número excessivo de redirecionamentos")

    def consumir[T](self, url: str, hosts: set[str], consume: Callable[[httpx.Response], T]) -> T:
        """Repete falhas transitórias, inclusive quando a conexão cai no download.

        Args:
            url: Origem HTTPS da resposta.
            hosts: Hosts autorizados, inclusive nos redirecionamentos.
            consume: Função que consome a resposta antes de seu fechamento;
                deve tolerar uma nova tentativa após falha parcial de rede.

        Returns:
            O valor retornado por ``consume`` na tentativa bem-sucedida.

        Raises:
            httpx.HTTPError: Falha permanente ou esgotamento de quatro tentativas.
            ValueError: URL ou conteúdo rejeitado; não é repetido automaticamente.
        """
        for attempt in range(4):
            delay = 2**attempt
            try:
                with_response = self._send(url, hosts)
                try:
                    with_response.raise_for_status()
                    return consume(with_response)
                finally:
                    retry_after = with_response.headers.get("Retry-After", "")
                    if retry_after.isdecimal():
                        delay = max(delay, min(int(retry_after), 120))
                    with_response.close()
            except httpx.HTTPStatusError as exc:
                motivo = motivo_http(exc)
                if exc.response.status_code not in RETRY_CODES or attempt == 3:
                    raise
            except httpx.TransportError as exc:
                motivo = motivo_http(exc)
                if attempt == 3:
                    raise
            LOG.warning("Falha transitória (%s). Nova tentativa em %ss.", motivo, delay)
            time.sleep(delay)
        raise RuntimeError("Tentativas HTTP esgotadas")

    def _pagina(self, params: dict[str, str | int], *, contexto: str) -> tuple[list[Registro], int]:
        """Lê uma página limitada em bytes e valida sua estrutura e contagem."""
        url = f"{self.base}/gazettes?{httpx.QueryParams(params)}"

        def read_page(response: httpx.Response) -> object:
            """Limita a resposta descomprimida antes de interpretar o JSON."""
            conteudo = bytearray()
            for bloco in response.iter_bytes():
                conteudo.extend(bloco)
                if len(conteudo) > MAX_PAGE:
                    raise ValueError("Página da API excede o tamanho aceito")
            return json.loads(conteudo)

        try:
            data = self.consumir(url, self.api_hosts, read_page)
        except httpx.HTTPError as exc:
            raise ErroListagem(f"{contexto}: {motivo_http(exc)}") from exc
        except ValueError as exc:
            raise ErroListagem(f"{contexto}: resposta inválida da API") from exc
        if not isinstance(data, dict):
            raise ErroListagem(f"{contexto}: resposta inválida da API")
        rows, total = data.get("gazettes"), data.get("total_gazettes")
        if not isinstance(rows, list) or type(total) is not int or total < 0:
            raise ErroListagem(f"{contexto}: resposta inválida da API")
        if any(not isinstance(row, dict) for row in rows) or len(rows) > total:
            raise ErroListagem(f"{contexto}: resposta inconsistente da API")
        return rows, total

    def cobertura(self, municipio: str, inicio: date, fim: date) -> Cobertura:
        """Consulta a última publicação no acervo e a contagem do período.

        Args:
            municipio: Código IBGE com sete dígitos.
            inicio: Primeiro dia do período, inclusive.
            fim: Último dia do período, inclusive.

        Returns:
            Última data, contagem e erros independentes de cada consulta. Uma
            contagem igual a zero é válida; ``None`` indica consulta sem sucesso.

        Raises:
            ValueError: O início é posterior ao fim.
        """
        if inicio > fim:
            raise ValueError("A data inicial deve ser igual ou anterior à final")
        base: dict[str, str | int] = {
            "territory_ids": municipio,
            "sort_by": "descending_date",
            "size": 1,
        }
        resultado: Cobertura = {
            "municipio_id": municipio,
            "ultima_publicacao": None,
            "registros": None,
            "erros": [],
        }
        consultas = (
            (base, "última publicação"),
            (
                dict(base, published_since=inicio.isoformat(), published_until=fim.isoformat()),
                "período selecionado",
            ),
        )
        for params, etapa in consultas:
            try:
                rows, total = self._pagina(params, contexto=etapa)
                if bool(rows) != bool(total):
                    raise ErroListagem(f"{etapa}: resposta inconsistente da API")
                if rows:
                    row = rows[0]
                    if str(row["territory_id"]) != municipio:
                        raise ValueError("Município fora do recorte")
                    publicada = date.fromisoformat(row["date"])
                if etapa == "última publicação":
                    if rows:
                        resultado["ultima_publicacao"] = publicada.isoformat()
                else:
                    if rows and not inicio <= publicada <= fim:
                        raise ValueError("Data fora do recorte")
                    resultado["registros"] = total
            except ErroListagem as exc:
                resultado["erros"].append(str(exc))
            except (KeyError, TypeError, ValueError):
                resultado["erros"].append(f"{etapa}: resposta inválida da API")
        return resultado

    def edicoes(self, municipio: str, inicio: date, fim: date, size: int) -> Iterator[Registro]:
        """Percorre janelas diárias para evitar offsets profundos.

        Args:
            municipio: Código IBGE consultado.
            inicio: Primeiro dia da janela, inclusive.
            fim: Último dia da janela, inclusive.
            size: Quantidade solicitada por página, entre 1 e 50.

        Yields:
            Registros na ordem fornecida pela fonte. Cada registro ainda precisa
            da validação de campos de ``validar_edicao`` antes da ingestão.

        Raises:
            ErroListagem: Falha HTTP, resposta inválida ou paginação inconsistente.
            ValueError: Período ou tamanho de página inválido.
        """
        if inicio > fim or not 1 <= size <= 50:
            raise ValueError("Período ou tamanho de página inválido")
        dia = inicio
        while dia <= fim:
            offset = 0
            paginas: set[str] = set()
            while True:
                rows, total = self._pagina(
                    {
                        "territory_ids": municipio,
                        "published_since": dia.isoformat(),
                        "published_until": dia.isoformat(),
                        "sort_by": "ascending_date",
                        "size": size,
                        "offset": offset,
                    },
                    contexto=f"{municipio} em {dia}, offset {offset}",
                )
                if not rows:
                    if offset < total:
                        raise ErroListagem(f"Paginação incompleta para {municipio} em {dia}")
                    break
                pagina = sha256(json.dumps(rows, sort_keys=True).encode())
                if pagina in paginas or offset + len(rows) > total:
                    raise ErroListagem(f"Paginação inconsistente para {municipio} em {dia}")
                paginas.add(pagina)
                for row in rows:
                    if (
                        str(row.get("territory_id")) != municipio
                        or row.get("date") != dia.isoformat()
                    ):
                        raise ErroListagem("A API retornou uma edição fora do recorte solicitado")
                    yield row
                offset += len(rows)
                if offset >= total:
                    break
            if dia == fim:
                break
            dia += timedelta(days=1)

    def baixar(self, url: str, raiz: Path, *, texto: bool = False) -> Arquivo:
        """Baixa em streaming, valida o conteúdo e publica o arquivo de forma atômica."""

        def save(response: httpx.Response) -> Arquivo:
            """Rejeita respostas parciais antes do arquivamento atômico."""
            if response.status_code == 206:
                raise ValueError("A origem retornou apenas parte do arquivo")
            return arquivar(raiz, response.iter_bytes(), texto=texto)

        return self.consumir(url, self.file_hosts, save)


def arquivar(raiz: Path, blocos: Iterable[bytes], *, texto: bool) -> Arquivo:
    """Deduplica por conteúdo, independentemente de município ou URL.

    Args:
        raiz: Diretório do acervo, criado se necessário.
        blocos: Bytes recebidos em streaming; não precisam caber juntos na memória.
        texto: Habilita validação textual e o limite de 25 MiB, em vez de 150 MiB.

    Returns:
        Checksum, caminho relativo, tipo detectado e tamanho do arquivo publicado.

    Raises:
        ValueError: Conteúdo vazio, HTML, TXT inválido ou tamanho acima do limite.
        OSError: Falha ao gravar ou publicar o arquivo. Temporários são removidos
            mesmo quando a leitura do iterável falha.
    """
    raiz.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    tamanho = 0
    with tempfile.NamedTemporaryFile(dir=raiz, delete=False) as handle:
        temporario = Path(handle.name)
        try:
            for bloco in blocos:
                tamanho += len(bloco)
                if tamanho > (MAX_TEXT if texto else MAX_FILE):
                    raise ValueError("Arquivo excede o tamanho aceito")
                digest.update(bloco)
                handle.write(bloco)
            handle.close()
            with temporario.open("rb") as sample:
                inicio = sample.read(1024).lstrip().lower()
            if not inicio or inicio.startswith((b"<!doctype html", b"<html")):
                raise ValueError("A origem retornou arquivo vazio ou página HTML")
            if texto:
                carregar_texto(temporario)
                tipo, ext = "text/plain", ".txt"
            elif inicio.startswith(b"%pdf-"):
                tipo, ext = "application/pdf", ".pdf"
            else:
                tipo, ext = "application/octet-stream", ".bin"
            checksum = digest.hexdigest()
            relativo = Path("objetos") / checksum[:2] / (checksum + ext)
            destino = raiz / relativo
            destino.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporario, destino)
            return Arquivo(checksum, relativo.as_posix(), tipo, tamanho)
        finally:
            temporario.unlink(missing_ok=True)


def carregar_texto(path: Path) -> str:
    """Normaliza o texto para Postgres, preservando os bytes originais no acervo."""
    raw = path.read_bytes()
    try:
        texto = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = raw.decode("cp1252", errors="replace")
    if "\x00" in texto or raw.lstrip().startswith(b"%PDF"):
        raise ValueError("O conteúdo retornado como TXT não é texto válido")
    if not texto.strip():
        raise ValueError("TXT sem conteúdo indexável")
    return texto
