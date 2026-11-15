"""Cliente HTTP com paginação, retentativas e armazenamento por SHA-256."""

import hashlib
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

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
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or parsed.hostname not in hosts
        or parsed.username or parsed.password or parsed.port not in (None, 443)
    ):
        raise ValueError("URL fora dos hosts HTTPS autorizados para o Querido Diário")


def motivo_http(exc: httpx.HTTPError) -> str:
    """Resume o erro sem expor URL, parâmetros ou conteúdo da resposta."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


class ErroListagem(RuntimeError):
    """Identifica a página que falhou após as tentativas da API."""


class Fonte:
    """Compartilha uma conexão e limita a taxa inclusive nas retentativas."""

    def __init__(self, client: httpx.Client, base: str, interval: float, extra_hosts=()):
        self.client = client
        self.base = base.rstrip("/")
        self.interval = interval
        self.next_request = 0.0
        self.api_hosts = {urlsplit(self.base).hostname, "api.queridodiario.ok.org.br",
                          "api.queridodiario.org.br"}
        self.file_hosts = FILE_HOSTS | set(extra_hosts)

    def _send(self, url: str, hosts: set[str]) -> httpx.Response:
        for _ in range(6):
            _validate_url(url, hosts)
            time.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + self.interval
            response = self.client.send(self.client.build_request("GET", url), stream=True)
            if not response.is_redirect or "location" not in response.headers:
                return response
            url = urljoin(url, response.headers["location"])
            response.close()
        raise ValueError("Número excessivo de redirecionamentos")

    def consumir(self, url, hosts, consume):
        """Repete falhas transitórias, inclusive quando a conexão cai no download."""
        for attempt in range(4):
            delay = 2 ** attempt
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

    def _pagina(self, params, *, contexto):
        url = f"{self.base}/gazettes?{httpx.QueryParams(params)}"

        def read_page(response):
            response.read()
            return response.json()

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
        return rows, total

    def cobertura(self, municipio: str, inicio: date, fim: date) -> dict:
        """Consulta a última publicação no acervo e a contagem do período."""
        base = {"territory_ids": municipio, "sort_by": "descending_date", "size": 1}
        resultado = {"municipio_id": municipio, "ultima_publicacao": None, "registros": None,
                     "erros": []}
        consultas = (
            (base, "última publicação"),
            (dict(base, published_since=inicio.isoformat(), published_until=fim.isoformat()),
             "período selecionado"),
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

    def edicoes(self, municipio: str, inicio: date, fim: date, size: int):
        """Percorre janelas diárias para evitar offsets profundos e limitar reprocessamento."""
        dia = inicio
        while dia <= fim:
            offset = 0
            while True:
                rows, total = self._pagina(
                    {"territory_ids": municipio, "published_since": dia.isoformat(),
                     "published_until": dia.isoformat(), "sort_by": "ascending_date",
                     "size": size, "offset": offset},
                    contexto=f"{municipio} em {dia}, offset {offset}",
                )
                if not rows:
                    if offset < total:
                        raise ValueError(f"Paginação incompleta para {municipio} em {dia}")
                    break
                for row in rows:
                    if str(row["territory_id"]) != municipio or row["date"] != dia.isoformat():
                        raise ValueError("A API retornou uma edição fora do recorte solicitado")
                    yield row
                offset += len(rows)
                if offset >= total:
                    break
            dia += timedelta(days=1)

    def baixar(self, url: str, raiz: Path, *, texto: bool = False) -> Arquivo:
        """Baixa em streaming, valida o conteúdo e publica o arquivo de forma atômica."""
        def save(response):
            if response.status_code == 206:
                raise ValueError("A origem retornou apenas parte do arquivo")
            return arquivar(raiz, response.iter_bytes(), texto=texto)
        return self.consumir(url, self.file_hosts, save)


def arquivar(raiz: Path, blocos, *, texto: bool) -> Arquivo:
    """Deduplica por conteúdo, independentemente de município ou URL."""
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
