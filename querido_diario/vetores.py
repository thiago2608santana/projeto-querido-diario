"""Indexação densa em Pinecone e recuperação de citações no Neon."""

import os
from datetime import date

from . import banco
from .fonte import sha256

EPOCH = date(1970, 1, 1)
BATCH_SIZE = 32


def exigir(nome):
    """Lê configuração sem imprimir o valor, mesmo em caso de erro."""
    valor = os.getenv(nome, "").strip()
    if not valor:
        raise ValueError(f"Configure {nome} no ambiente ou .env")
    return valor


class BuscaVetorial:
    """Usa o mesmo provedor, modelo e namespace para gravar e consultar."""

    def __init__(self):
        from pinecone import Pinecone

        self.provider = os.getenv("EMBEDDING_PROVIDER", "openai").strip().lower()
        if self.provider == "openai":
            from openai import OpenAI

            self.model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
            self.embed_client = OpenAI(api_key=exigir("OPENAI_API_KEY"), timeout=45)
        elif self.provider == "ollama":
            from ollama import Client

            self.model = exigir("OLLAMA_EMBEDDING_MODEL")
            self.embed_client = Client(host=os.getenv("OLLAMA_HOST", "http://localhost:11434"), timeout=60)
        else:
            raise ValueError("EMBEDDING_PROVIDER deve ser openai ou ollama")
        self.pc = Pinecone(api_key=exigir("PINECONE_API_KEY"), timeout=45)
        description = self.pc.describe_index(exigir("PINECONE_INDEX_NAME"))
        self.dim = description.dimension
        if not self.dim or getattr(description, "vector_type", "dense") != "dense":
            raise ValueError("Configure um índice denso no Pinecone")
        nome = os.getenv("PINECONE_NAMESPACE", "querido-diario").strip()
        identidade = f"{self.provider}:{self.model}:{self.dim}"
        # Isola espaços vetoriais de modelos diferentes, mesmo com a mesma dimensão.
        self.namespace = f"{nome}-{sha256(identidade.encode())[:12]}"
        self.perfil = sha256(f"{description.host}:{self.namespace}:{identidade}".encode())
        self.index = self.pc.Index(host=description.host)

    def embeddings(self, textos):
        """Gera vetores na ordem dos textos e valida a dimensão antes de escrever."""
        if self.provider == "openai":
            result = self.embed_client.embeddings.create(model=self.model, input=textos)
            valores = [item.embedding for item in sorted(result.data, key=lambda item: item.index)]
        else:
            valores = self.embed_client.embed(model=self.model, input=textos, truncate=False).embeddings
        if len(valores) != len(textos) or any(len(v) != self.dim for v in valores):
            raise ValueError(f"O modelo de embeddings deve produzir vetores de dimensão {self.dim}")
        return valores

    def indexar(self, db, maximo):
        """Confirma cada lote depois do upsert. Uma nova execução retoma os pendentes."""
        feitos = 0
        while feitos < maximo:
            with banco.exclusivo(db, "vetores:" + self.perfil):
                lote = banco.pendentes(db, self.perfil, min(BATCH_SIZE, maximo - feitos))
                if not lote:
                    break
                vetores = [
                    {
                        "id": row["id"], "values": valor,
                        "metadata": {
                            "municipio_id": row["municipio_id"],
                            "dia": (row["publicada_em"] - EPOCH).days,
                        },
                    }
                    for row, valor in zip(lote, self.embeddings([r["texto"] for r in lote]), strict=True)
                ]
                resposta = self.index.upsert(vectors=vetores, namespace=self.namespace)
                if resposta.upserted_count != len(vetores):
                    raise RuntimeError("Pinecone não confirmou todos os vetores do lote")
                banco.confirmar(db, self.perfil, [row["id"] for row in lote])
                feitos += len(lote)
        return feitos

    def buscar(self, db, consulta, recorte, limite):
        """Filtra no índice e usa o Neon como fonte do texto e dos metadados."""
        filtro = {}
        if recorte["municipio"]:
            filtro["municipio_id"] = {"$eq": recorte["municipio"]}
        dias = {}
        for campo, operador in (("inicio", "$gte"), ("fim", "$lte")):
            if recorte[campo]:
                dias[operador] = (recorte[campo] - EPOCH).days
        if dias:
            filtro["dia"] = dias
        opcoes = {"filter": filtro} if filtro else {}
        response = self.index.query(
            namespace=self.namespace, vector=self.embeddings([consulta])[0],
            top_k=min(limite * 3, 300), include_metadata=False, **opcoes,
        )
        rows = banco.recuperar(db, [m.id for m in response.matches], recorte)
        return [dict(rows[m.id], score=m.score) for m in response.matches if m.id in rows][:limite]


def combinar(lexicais, semanticos, limite):
    """Funde posições com RRF, sem misturar escalas de pontuação dos dois motores."""
    grupos = {}
    for origem, lista in (("lexical", lexicais), ("semantica", semanticos)):
        for posicao, row in enumerate(lista, 1):
            key = row["trecho_id"]
            if key not in grupos:
                grupos[key] = dict(row, score=0.0, fontes=[])
            grupos[key]["fontes"].append(origem)
            grupos[key]["score"] += 1 / (60 + posicao)
    return sorted(grupos.values(), key=lambda item: (-item["score"], item["trecho_id"]))[:limite]
