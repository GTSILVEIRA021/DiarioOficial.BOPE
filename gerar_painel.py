#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_painel.py
Lê o CSV raspado do Diário Oficial do RJ, calcula os agregados e injeta tudo
num template HTML autocontido.

Uso:
    python3 gerar_painel.py noticias_bope_doerj.csv
    python3 gerar_painel.py entrada.csv -t template.html -o painel.html

Dependência única: pandas.
    pip install pandas
"""

import argparse
import json
import sys
import unicodedata
from pathlib import Path

import pandas as pd


# ----------------------------------------------------------------------------
# CONFIGURAÇÃO — é aqui que você mexe
# ----------------------------------------------------------------------------

# Campos semânticos da sonda léxica. Cada campo é um rótulo + lista de padrões.
# Os padrões são casados SEM acento e em minúsculas, então escreva-os assim.
# Um ato conta uma vez por campo, mesmo que dê match em vários padrões.
SONDAS = [
    ("operacao policial",      ["operacao ", "operacoes "]),
    ("contrato / aditivo",     ["contrato", "aditivo"]),
    ("licitacao / pregao",     ["licita", "pregao", "edital de"]),
    ("curso / formacao",       ["curso", "treinamento", "estagio"]),
    ("aquisicao / material",   ["aquisic", "aquisit", "material belico", "armamento", "municao"]),
    ("elogio / congratulacao", ["congratula", "louvor", "medalha", "merito"]),
    ("gratificacao / soldo",   ["gratifica", "soldo", "remunera"]),
    ("morte / vitima",         ["morte", "morto", "obito", "vitima", "falecim"]),
    ("apuracao / disciplina",  ["sindicancia", "corregedoria", "processo administrativo",
                                "disciplinar", "inquerito"]),
    ("violacao / abuso",       ["violacao", "abuso", "arbitrar", "tortura", "excesso"]),
    ("direitos humanos",       ["direitos humanos", "direito humano"]),
    ("letalidade",             ["letalidade", "letal"]),
]

# Marcadores históricos exibidos abaixo do gráfico de barras.
EVENTOS = [
    (2007, "Alemão + estreia de Tropa de Elite"),
    (2008, "Início das UPPs (Santa Marta)"),
    (2010, "Ocupação do Complexo do Alemão"),
    (2013, "Caso Amarildo / Jornadas de Junho"),
    (2014, "Ocupação da Maré"),
    (2016, "Olimpíadas"),
    (2018, "Intervenção federal na segurança"),
    (2019, "Gestão Witzel / recorde de letalidade"),
    (2020, "Pandemia / liminar da ADPF 635"),
    (2021, "Chacina do Jacarezinho"),
    (2022, "Vila Cruzeiro e Alemão"),
    (2025, "ADPF 635 julgada / Operação Contenção"),
]

# Ordem das faixas no gráfico empilhado. Precisa bater com o CSS do template.
PODERES = ["Executivo", "Legislativo", "Judiciário", "Outros órgãos"]

N_TIPOS = 14  # quantas espécies de ato listar na seção 03


# ----------------------------------------------------------------------------
# FUNÇÕES
# ----------------------------------------------------------------------------

def sem_acento(s: str) -> str:
    """Remove acentos e baixa a caixa, para casar padrões de forma robusta."""
    s = str(s).lower()
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def classifica_poder(parte: str) -> str:
    """Mapeia o campo 'jornal' do IOERJ (Parte I, II, III...) para um poder."""
    if parte.startswith("Parte I ("):
        return "Executivo"
    if parte.startswith("Parte II "):
        return "Legislativo"
    if parte.startswith("Parte III"):
        return "Judiciário"
    return "Outros órgãos"


def limpa_titulo(titulo: str, tipo: str) -> str:
    """O título do IOERJ repete a espécie do ato como prefixo. Remove."""
    prefixo = f"{tipo} - "
    if titulo.startswith(prefixo):
        titulo = titulo[len(prefixo):]
    return titulo.strip(" -")


def carrega(caminho: Path) -> pd.DataFrame:
    # utf-8-sig porque o arquivo vem com BOM
    df = pd.read_csv(caminho, encoding="utf-8-sig")

    obrigatorias = {"titulo", "data_publicacao", "jornal", "tipo",
                    "pagina", "termo_busca", "trecho", "link"}
    faltando = obrigatorias - set(df.columns)
    if faltando:
        sys.exit(f"Erro: faltam as colunas {sorted(faltando)} no CSV.")

    df["data"] = pd.to_datetime(df["data_publicacao"], errors="coerce")
    invalidas = df["data"].isna().sum()
    if invalidas:
        print(f"  aviso: {invalidas} linhas com data ilegível, descartadas")
        df = df.dropna(subset=["data"])

    # Deduplica por id_materia se a coluna existir; senão, por link.
    chave = "id_materia" if "id_materia" in df.columns else "link"
    antes = len(df)
    df = df.drop_duplicates(subset=[chave])
    if len(df) < antes:
        print(f"  aviso: {antes - len(df)} duplicatas removidas por '{chave}'")

    df = df.sort_values("data", ascending=False).reset_index(drop=True)
    df["poder"] = df["jornal"].apply(classifica_poder)
    df["titulo_limpo"] = df.apply(
        lambda r: limpa_titulo(r["titulo"], r["tipo"]), axis=1)
    return df


def calcula_sonda(df: pd.DataFrame) -> list:
    """
    Conta em quantos atos cada campo semântico aparece.

    Varre título + trecho, ou seja, o TEXTO DE ORIGEM publicado no diário. O
    resumo fica deliberadamente de fora: ele é descrição curada, não fonte, e
    contá-lo mediria o vocabulário de quem resumiu, não o do Estado.

    ATENÇÃO METODOLÓGICA: se o trecho for o snippet de ~150 caracteres devolvido
    pela busca do IOERJ, um resultado zero significa 'não aparece no recorte
    indexado', NÃO 'ausente do documento'. Só vire a contagem em evidência
    depois de raspar o inteiro teor.
    """
    texto = (df["titulo"].fillna("") + " " + df["trecho"].fillna("")).apply(sem_acento)

    resultado = []
    for rotulo, padroes in SONDAS:
        mascara = pd.Series(False, index=texto.index)
        for p in padroes:
            mascara |= texto.str.contains(sem_acento(p), regex=False)
        resultado.append({"termo": rotulo, "n": int(mascara.sum())})

    resultado.sort(key=lambda x: -x["n"])
    return resultado


def calcula_serie(df: pd.DataFrame) -> list:
    """Contagem por ano, quebrada por poder — alimenta as barras empilhadas."""
    serie = []
    for ano in sorted(df["data"].dt.year.unique()):
        sub = df[df["data"].dt.year == ano]
        linha = {"ano": int(ano), "total": int(len(sub))}
        for p in PODERES:
            linha[p] = int((sub["poder"] == p).sum())
        serie.append(linha)
    return serie


def monta_payload(df: pd.DataFrame) -> dict:
    # O 'titulo' do IOERJ é o começo cru do ato, truncado no meio da frase.
    # O 'resumo' é a descrição curada — é ele que vira o título do cartão.
    registros = []
    for _, r in df.iterrows():
        resumo = str(r["resumo"]).strip() if pd.notna(r["resumo"]) else ""
        if not resumo:
            resumo = r["titulo_limpo"][:200]
        registros.append({
            "d": r["data"].strftime("%Y-%m-%d"),
            "t": resumo[:220],
            "x": (r["trecho"] if isinstance(r["trecho"], str) else "")[:220],
            "p": r["poder"],
            "a": r["tipo"],
            "g": r["pagina"],
            "u": r["link"],
        })

    tipos = (df["tipo"].value_counts().head(N_TIPOS)
             .rename_axis("tipo").reset_index(name="n").to_dict("records"))
    for t in tipos:
        t["n"] = int(t["n"])

    return {
        "meta": {
            "n": int(len(df)),
            "ini": df["data"].min().strftime("%d/%m/%Y"),
            "fim": df["data"].max().strftime("%d/%m/%Y"),
            "anos": [int(df["data"].dt.year.min()), int(df["data"].dt.year.max())],
            "trecho_medio": int(df["trecho"].fillna("").str.len().mean()),
            "n_tipos": int(df["tipo"].nunique()),
        },
        "serie": calcula_serie(df),
        "poderes": PODERES,
        "tipos": tipos,
        "probe": calcula_sonda(df),
        "eventos": [{"ano": a, "rotulo": r} for a, r in EVENTOS],
        "registros": registros,
    }


def escreve_html(payload: dict, template: Path, saida: Path) -> None:
    html = template.read_text(encoding="utf-8")
    if "__DATA__" not in html:
        sys.exit(f"Erro: o template {template} não contém o marcador __DATA__.")

    # separators sem espaço enxuga bastante o arquivo final
    dados = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    # Escapa '</' para nenhum '</script>' dentro dos dados fechar a tag
    # antes da hora. O JSON continua válido: '<\/' é escape legal.
    dados = dados.replace("</", "<\\/")

    saida.write_text(html.replace("__DATA__", dados), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Gera o painel HTML do corpus DOERJ.")
    ap.add_argument("csv", type=Path, help="CSV raspado do IOERJ")
    ap.add_argument("-t", "--template", type=Path, default=Path("template.html"))
    ap.add_argument("-o", "--saida", type=Path, default=Path("painel_bope_doerj.html"))
    args = ap.parse_args()

    print(f"lendo {args.csv}")
    df = carrega(args.csv)
    print(f"  {len(df)} atos, {df['data'].min():%Y} a {df['data'].max():%Y}")

    payload = monta_payload(df)

    print("\nsonda léxica:")
    for p in payload["probe"]:
        aviso = "   <- ausente no recorte indexado" if p["n"] == 0 else ""
        print(f"  {p['termo']:26s} {p['n']:5d}{aviso}")

    escreve_html(payload, args.template, args.saida)
    kb = args.saida.stat().st_size / 1024
    print(f"\ngravado: {args.saida}  ({kb:.0f} KB)")


if __name__ == "__main__":
    main()
