"""Painel local de coleta do Querido Diário, usando a CLI como executora."""

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

import streamlit as st

ROOT = Path(__file__).resolve().parent
# Não resolva o symlink: o caminho em .venv identifica o ambiente virtual.
PIPELINE_PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
DATE_ZONE = ZoneInfo("America/Sao_Paulo")
MAX_MANIFEST = 5 * 1024 * 1024


class Execucao:
    """Executa uma tarefa por vez e mantém o último relatório durante a sessão do servidor."""

    def __init__(self):
        self.lock = Lock()
        self.pasta = tempfile.TemporaryDirectory(prefix="qd-painel-")
        self.raiz = Path(self.pasta.name)
        self.processo = None
        self.titulo = ""
        self.cancelada = False

    def iniciar(self, titulo, argumentos, manifesto=None):
        """Inicia a CLI sem shell, com a mesma configuração usada no terminal."""
        with self.lock:
            if self.processo and self.processo.poll() is None:
                raise ValueError("Já existe uma tarefa em execução.")
            if manifesto is not None:
                path = self.raiz / "manifesto.json"
                path.write_bytes(manifesto)
                argumentos = [*argumentos, "--manifesto", str(path)]
            python = PIPELINE_PYTHON if PIPELINE_PYTHON.is_file() else sys.executable
            with (self.raiz / "resultado").open("wb") as saida, (self.raiz / "log").open("wb") as log:
                self.processo = subprocess.Popen(
                    [str(python), "-E", "-u", "-m", "querido_diario", *argumentos],
                    cwd=ROOT, stdout=saida, stderr=log,
                    env={**os.environ, "QD_LOG_LEVEL": "INFO"},
                )
            self.titulo = titulo
            self.cancelada = False

    def cancelar(self):
        """Interrompe o processo e deixa os dados já confirmados disponíveis."""
        with self.lock:
            if self.processo and self.processo.poll() is None:
                try:
                    self.processo.terminate()
                except ProcessLookupError:
                    return
                self.cancelada = True

    def estado(self):
        """Retorna status, resumo JSON e o fim do log, sem carregar logs grandes."""
        with self.lock:
            if self.processo is None:
                return None
            codigo = self.processo.poll()
            with (self.raiz / "log").open("rb") as log:
                log.seek(max(0, os.fstat(log.fileno()).st_size - 16000))
                mensagens = log.read().decode("utf-8", errors="replace")
            resumo = {}
            if codigo is not None:
                bruto = (self.raiz / "resultado").read_text(encoding="utf-8", errors="replace")
                try:
                    resumo = json.loads(bruto)
                except ValueError:
                    pass
            return self.titulo, codigo, self.cancelada, resumo, mensagens


@st.cache_resource
def executor():
    """Compartilha a execução entre abas do mesmo servidor local."""
    return Execucao()


def codigos(entrada, obrigatorio):
    """Aceita códigos IBGE separados por espaço, vírgula ou quebra de linha."""
    itens = list(dict.fromkeys(re.findall(r"[^\s,]+", entrada)))
    if obrigatorio and not itens:
        raise ValueError("Informe pelo menos um código IBGE.")
    if any(not re.fullmatch(r"[0-9]{7}", item) for item in itens):
        raise ValueError("Cada código IBGE deve ter sete dígitos.")
    return itens


def datas(inicio, fim):
    """Converte datas opcionais para argumentos e valida a ordem do período."""
    if inicio and fim and inicio > fim:
        raise ValueError("A data inicial deve ser anterior ou igual à final.")
    return [item for nome, valor in (("--inicio", inicio), ("--fim", fim))
            if valor for item in (nome, valor.isoformat())]


def coleta(painel, em_execucao):
    """Oferece coleta por municípios e datas ou por manifesto JSON."""
    origem = st.radio("Origem", ["Municípios e período", "Manifesto JSON"], horizontal=True)
    hoje = datetime.now(DATE_ZONE).date()
    with st.form("coleta"):
        if origem == "Municípios e período":
            municipios = st.text_input("Códigos IBGE", placeholder="Ex.: 5208707, 3509502")
            esquerda, direita = st.columns(2)
            inicio = esquerda.date_input("Data inicial", hoje - timedelta(days=6), format="DD/MM/YYYY")
            fim = direita.date_input("Data final", hoje, format="DD/MM/YYYY")
            arquivo = None
        else:
            arquivo = st.file_uploader("Manifesto com a lista de edições", type=["json"])
        storage = st.checkbox("Enviar PDFs ao bucket configurado", value=True)
        docling = st.checkbox("Extrair texto com Docling quando o TXT estiver indisponível")
        iniciar = st.form_submit_button("Iniciar coleta", type="primary", disabled=em_execucao)
        verificar = origem == "Municípios e período" and st.form_submit_button(
            "Verificar cobertura", disabled=em_execucao,
        )
    if verificar:
        try:
            argumentos = ["cobertura", *datas(inicio, fim)]
            for codigo in codigos(municipios, True):
                argumentos.extend(["--municipio", codigo])
            painel.iniciar("Cobertura no Querido Diário", argumentos)
        except (ValueError, OSError) as exc:
            st.error(str(exc))
        else:
            st.rerun()
    if iniciar:
        try:
            argumentos = ["sync"]
            if not storage:
                argumentos.append("--sem-storage")
            if docling:
                argumentos.append("--docling")
            manifesto = None
            if origem == "Municípios e período":
                for codigo in codigos(municipios, True):
                    argumentos.extend(["--municipio", codigo])
                argumentos.extend(datas(inicio, fim))
            else:
                if arquivo is None or arquivo.size > MAX_MANIFEST:
                    raise ValueError("Selecione um manifesto JSON de até 5 MB.")
                manifesto = arquivo.getvalue()
                dados = json.loads(manifesto)
                if not isinstance(dados, dict) or not isinstance(dados.get("gazettes"), list):
                    raise ValueError("O manifesto precisa ter uma lista no campo gazettes.")
            painel.iniciar("Coleta de edições", argumentos, manifesto)
        except (ValueError, OSError) as exc:
            st.error(str(exc))
        else:
            st.rerun()


def enviar_pdfs(painel, em_execucao):
    """Permite reenviar os PDFs locais pendentes sem consultar a API de listagem."""
    st.write("Envie os PDFs já coletados que ainda aguardam uma cópia no bucket.")
    with st.form("envio"):
        municipio = st.text_input("Código IBGE (opcional)", placeholder="Ex.: 5208707")
        esquerda, direita = st.columns(2)
        inicio = esquerda.date_input("Data inicial (opcional)", value=None, format="DD/MM/YYYY")
        fim = direita.date_input("Data final (opcional)", value=None, format="DD/MM/YYYY")
        maximo = st.number_input("Máximo de edições", min_value=1, max_value=100000, value=500)
        enviar = st.form_submit_button("Enviar PDFs", type="primary", disabled=em_execucao)
    if enviar:
        try:
            municipios = codigos(municipio, False)
            if len(municipios) > 1:
                raise ValueError("Informe um único código IBGE para filtrar o envio.")
            argumentos = ["enviar-pdfs", "--maximo", str(maximo), *datas(inicio, fim)]
            if municipios:
                argumentos.extend(["--municipio", municipios[0]])
            painel.iniciar("Envio de PDFs", argumentos)
        except (ValueError, OSError) as exc:
            st.error(str(exc))
        else:
            st.rerun()


def interface():
    """Renderiza os controles e acompanha a última tarefa com atualização periódica."""
    st.set_page_config(page_title="Querido Diário · Coleta", page_icon="📄", layout="centered")
    st.title("Querido Diário")
    st.caption("Coleta de edições municipais e envio de PDFs ao Neon")
    painel = executor()

    @st.fragment(run_every=1 if (estado := painel.estado()) and estado[1] is None else None)
    def controles():
        estado_atual = painel.estado()
        executando = bool(estado_atual and estado_atual[1] is None)
        if estado and estado[1] is None and not executando:
            st.rerun()
        acao = st.radio("Operação", ["Coletar edições", "Enviar PDFs pendentes"], horizontal=True)
        if acao == "Coletar edições":
            coleta(painel, executando)
        else:
            enviar_pdfs(painel, executando)
        if not estado_atual:
            return
        titulo, codigo, cancelada, resumo, log = estado_atual
        st.divider()
        st.subheader(titulo)
        if codigo is None:
            st.info("Em andamento…" if not cancelada else "Interrompendo…")
            if st.button("Interromper execução", disabled=cancelada):
                painel.cancelar()
                st.rerun()
        elif cancelada:
            st.warning("Execução interrompida. Os dados já confirmados foram preservados.")
        elif codigo == 0:
            st.success("Execução concluída.")
        elif resumo:
            st.warning("Execução concluída com falhas. Consulte o registro abaixo.")
        else:
            st.error("A execução falhou antes de gerar um resumo.")
        if "cobertura" in resumo:
            inicio = resumo["inicio"]
            st.caption(f"Período consultado: {inicio} a {resumo['fim']}. "
                       "As datas refletem o acervo do Querido Diário.")
            for row in resumo["cobertura"]:
                st.markdown(f"**IBGE {row['municipio_id']}**")
                ultima = row["ultima_publicacao"]
                registros = row["registros"]
                if ultima:
                    st.write(f"Última publicação no acervo: {ultima}")
                elif not any(erro.startswith("última publicação") for erro in row["erros"]):
                    st.write("Nenhuma edição encontrada no acervo.")
                if registros is not None:
                    st.write(f"Registros no período: {registros}")
                    if registros == 0 and ultima and ultima < inicio:
                        st.warning("A última edição no acervo é anterior ao período selecionado.")
                for erro in row["erros"]:
                    st.error(erro)
        indicadores = [
            ("edicoes", "Edições"), ("pdfs", "PDFs locais"), ("textos", "Textos"),
            ("pdfs_no_storage", "PDFs no bucket"), ("falhas", "Falhas"),
        ]
        visiveis = [(chave, label) for chave, label in indicadores if chave in resumo]
        if visiveis:
            for coluna, (chave, label) in zip(st.columns(len(visiveis)), visiveis, strict=True):
                coluna.metric(label, resumo[chave])
            with st.expander("Resumo completo"):
                st.json(resumo)
        with st.expander("Registro da execução", expanded=executando or bool(codigo)):
            if log:
                st.code(log, language=None)
            else:
                st.caption("Nenhuma mensagem registrada.")

    controles()


if __name__ == "__main__":
    interface()
