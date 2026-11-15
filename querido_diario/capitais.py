"""Códigos das capitais na tabela oficial de municípios do IBGE.

Fonte: https://www.ibge.gov.br/explica/codigos-dos-municipios.php
"""

CAPITAIS: dict[str, str] = {
    "2800308": "Aracaju (SE)",
    "1501402": "Belém (PA)",
    "3106200": "Belo Horizonte (MG)",
    "1400100": "Boa Vista (RR)",
    "5300108": "Brasília (DF)",
    "5002704": "Campo Grande (MS)",
    "5103403": "Cuiabá (MT)",
    "4106902": "Curitiba (PR)",
    "4205407": "Florianópolis (SC)",
    "2304400": "Fortaleza (CE)",
    "5208707": "Goiânia (GO)",
    "2507507": "João Pessoa (PB)",
    "1600303": "Macapá (AP)",
    "2704302": "Maceió (AL)",
    "1302603": "Manaus (AM)",
    "2408102": "Natal (RN)",
    "1721000": "Palmas (TO)",
    "4314902": "Porto Alegre (RS)",
    "1100205": "Porto Velho (RO)",
    "2611606": "Recife (PE)",
    "1200401": "Rio Branco (AC)",
    "3304557": "Rio de Janeiro (RJ)",
    "2927408": "Salvador (BA)",
    "2111300": "São Luís (MA)",
    "3550308": "São Paulo (SP)",
    "2211001": "Teresina (PI)",
    "3205309": "Vitória (ES)",
}


def rotulo_municipio(codigo: str) -> str:
    """Mostra a capital quando conhecida, com o código sempre visível."""
    nome = CAPITAIS.get(codigo)
    return f"{nome} · {codigo}" if nome else f"IBGE {codigo}"
