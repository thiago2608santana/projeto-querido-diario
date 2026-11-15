# Revisão técnica do pipeline

Revisão de 15/11/2026: módulos de produção, CLI, painel Streamlit, testes,
esquema SQL, configuração e documentação. O notebook de exploração não contém
funções próprias a padronizar. Não foram executadas coletas nem escritas nos
serviços externos durante a revisão.

## Falhas corrigidas

| Prioridade | Situação anterior | Comportamento após a revisão |
| --- | --- | --- |
| Alta | Reutilização de PDF pelo tamanho e de TXT apenas pela existência; conteúdo alterado com o mesmo tamanho passava despercebido. | Verifica SHA-256 de ambos os arquivos antes de reutilizar; conteúdo divergente é baixado novamente. |
| Alta | A leitura do cache da coleta aceitava caminhos que saíam do acervo, inclusive por link simbólico. | Resolve o caminho e exige que permaneça dentro da raiz do acervo. O envio S3 já fazia essa verificação. |
| Alta | Um cliente HTTP configurado com redirecionamentos automáticos podia contornar a validação de cada destino. | Força `follow_redirects=False` em cada envio e valida o destino antes de segui-lo. |
| Média | Uma página com registros não objetos ou repetida podia causar erro não tratado ou reprocessamento. | Valida estrutura e contagem; detecta páginas repetidas e offsets incompatíveis e informa `ErroListagem`. |
| Média | A resposta JSON da API era lida sem limite de tamanho. | Limita a página descomprimida a 10 MiB antes de interpretar o JSON. |
| Média | A CLI lia manifestos sem limite e só verificava sua estrutura depois de preparar os serviços. | Aplica o mesmo limite de 5 MiB do painel e valida a estrutura antes de preparar o esquema e o cliente S3. Campos de cada edição são validados antes do download. |
| Média | Embeddings eram validados apenas pela quantidade e dimensão. | Rejeita índices duplicados/ausentes e valores NaN/infinito antes da indexação; preserva a ordenação da entrada. |
| Baixa | O tamanho do upload dependia da posição do arquivo após `hashlib.file_digest`, que não garante essa posição. | Obtém o tamanho com `os.fstat` e reposiciona o arquivo antes do upload. |
| Baixa | Um JSON de resultado escalar ou lista podia quebrar a renderização do painel. | Aceita apenas objetos JSON como resumo e preserva o log para diagnóstico. |
| Baixa | Incrementar a última data possível em Python gerava overflow no fim da paginação. | Encerra a iteração ao concluir a data final. |

As verificações de integridade acrescentam leitura sequencial dos arquivos
reutilizados. Isso aumenta o trabalho de disco nas sincronizações repetidas,
mas permite detectar corrupção que a checagem de tamanho não identifica.

## Tipagem e documentação

- Todas as funções de produção e testes têm parâmetros e retornos anotados,
  incluindo auxiliares internas, e possuem docstrings em português.
- `tipos.py` centraliza contratos de entrada, recortes e resultados com `TypedDict`.
- `Registro` mantém `Any` para linhas heterogêneas SQL/JSON. Clientes dinâmicos
  de boto3 e o adaptador compartilhado entre OpenAI/Ollama também usam `Any`
  de forma explícita; isso não implica verificação estática completa desses SDKs.
- Ruff verifica imports, erros básicos, anotações e docstrings. A regra de modo
  imperativo em inglês não é aplicada ao texto em português.
- mypy verifica os dez arquivos de produção, incluindo o painel. A configuração
  não percorre as dependências dos SDKs de Docling, OpenAI e Ollama.
- Um teste com `ast` impede a introdução de funções sem docstring ou sem
  anotações. Ele não comprova a correção semântica dos tipos, papel do mypy.

Não houve mudança no esquema SQL, nas chaves de edição/trecho, no algoritmo
de divisão, nos nomes dos comandos ou no formato dos resumos JSON. Nenhuma
migração ou reindexação é necessária por causa desta revisão.

## Validação e limites

A suíte usa `httpx.MockTransport`, clientes simulados e diretórios temporários.
Cobre paginação, cobertura vazia versus falha, retentativas, limites de entrada,
integridade local, idempotência da coleta, confirmação de uploads, embeddings,
filtros e controlador do painel, além da padronização do código.

As verificações locais não comprovam conectividade, permissões, limites de
conta ou compatibilidade real dos serviços Neon, S3, Pinecone, OpenAI e Ollama.
A extração real com modelos do Docling também não integra a suíte. Uma
validação integrada posterior deve usar um banco/bucket de teste e um recorte
pequeno, acompanhando os custos e as referências persistidas.

Limitações operacionais que continuam relevantes:

- A contagem e a última publicação mostram o acervo do Querido Diário; não
  comprovam atualização em relação à prefeitura. Consultas sucessivas não são
  um snapshot atômico da API. Uma inconsistência exige repetir a janela.
- Os bloqueios transacionais abrangem operações de rede para impedir duas
  execuções sobre a mesma edição ou perfil. Arquivos lentos podem prolongar
  transações e exigir ajuste dos timeouts do banco.
- O banco registra uploads confirmados. Uma remoção posterior diretamente no
  bucket não é descoberta automaticamente quando a referência já coincide.
- Trechos substituídos deixam vetores antigos no Pinecone até manutenção do
  namespace. A busca descarta IDs ausentes no Neon, podendo retornar menos
  resultados que o limite solicitado. A exclusão remota deve considerar os
  perfis e namespaces utilizados antes de ser automatizada.
- A segmentação normaliza espaços e usa caracteres, não tokens nem páginas.
  Para texto integral fiel, utilize o TXT arquivado; para evidência visual,
  consulte o PDF original.
- O painel permanece local e sem autenticação; seu histórico é temporário.
