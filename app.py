# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import io
import re
import json
import time
import hashlib
import unicodedata
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
import requests
import streamlit as st

# =========================================================
# CONFIG
# =========================================================

st.set_page_config(
    page_title="Acerte Licitações",
    page_icon="📑",
    layout="wide"
)

ORIGIN = "https://pncp.gov.br"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124 Safari/537.36"
    ),
    "Accept": "application/json"
}

BASE_DIR = os.path.dirname(__file__)

DATA_DIR = os.path.join(BASE_DIR, "data")

CSV_PNCP = os.path.join(DATA_DIR, "ListaMunicipiosPNCP.csv")

STATUS_MAP = {
    "A Receber": "recebendo_proposta",
    "Em Julgamento": "em_julgamento",
    "Encerradas": "encerrado",
    "Todos": ""
}

# =========================================================
# HELPERS
# =========================================================

def norm(txt: str) -> str:

    if not txt:
        return ""

    txt = str(txt)

    txt = unicodedata.normalize("NFKD", txt)

    txt = txt.encode("ascii", "ignore").decode("utf-8")

    txt = txt.lower().strip()

    return txt


def fmt_data(valor: str) -> str:

    if not valor:
        return ""

    try:

        dt = pd.to_datetime(valor)

        return dt.strftime("%d/%m/%Y")

    except Exception:

        return valor


def build_uid(row: Dict) -> str:

    raw = (
        str(row.get("titulo", ""))
        + str(row.get("orgao", ""))
        + str(row.get("data_publicacao", ""))
    )

    return hashlib.md5(raw.encode()).hexdigest()


# =========================================================
# CSV MUNICÍPIOS
# =========================================================

@st.cache_data
def load_municipios() -> pd.DataFrame:

    encodings = [
        "utf-8",
        "utf-8-sig",
        "latin1",
        "cp1252"
    ]

    separators = [
        ",",
        ";",
        "\t",
        "|"
    ]

    for enc in encodings:

        for sep in separators:

            try:

                df = pd.read_csv(
                    CSV_PNCP,
                    dtype=str,
                    encoding=enc,
                    sep=sep,
                    engine="python"
                )

                if len(df.columns) < 2:
                    continue

                cols = {norm(c): c for c in df.columns}

                col_nome = None
                col_codigo = None
                col_uf = None

                for key in cols:

                    if key in ["municipio", "nome"]:
                        col_nome = cols[key]

                    if key in ["id", "codigo"]:
                        col_codigo = cols[key]

                    if key in ["uf", "estado"]:
                        col_uf = cols[key]

                if not col_nome or not col_codigo:
                    continue

                out = pd.DataFrame()

                out["nome"] = df[col_nome].astype(str)

                out["codigo"] = df[col_codigo].astype(str)

                if col_uf:
                    out["uf"] = df[col_uf].astype(str)
                else:
                    out["uf"] = ""

                out["nome_norm"] = out["nome"].map(norm)

                return out

            except Exception:
                continue

    raise Exception("Falha ao carregar ListaMunicipiosPNCP.csv")


# =========================================================
# PNCP API
# =========================================================

def extract_items(js) -> List[Dict]:

    if isinstance(js, list):
        return js

    if not isinstance(js, dict):
        return []

    keys = [
        "items",
        "data",
        "resultados",
        "content"
    ]

    for key in keys:

        val = js.get(key)

        if isinstance(val, list):
            return val

    return []


def consultar_pncp(
    codigo_municipio: str,
    status: str
) -> List[Dict]:

    pagina = 1

    resultados = []

    session = requests.Session()

    while True:

        url = "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"

        params = {
            "pagina": pagina,
            "tamanhoPagina": 50,
            "codigoMunicipioIbge": codigo_municipio
        }

        r = session.get(
            url,
            params=params,
            headers=HEADERS,
            timeout=30
        )

        if r.status_code != 200:
            break

        try:

            js = r.json()

        except Exception:
            break

        itens = extract_items(js)

        if not itens:
            break

        for item in itens:

            situacao = str(
                item.get("situacaoCompraNome", "")
            ).lower()

            if status:

                if status not in situacao:
                    continue

            resultados.append(item)

        if len(itens) < 50:
            break

        pagina += 1

        time.sleep(0.05)

    return resultados


# =========================================================
# TRANSFORMAÇÃO
# =========================================================

def montar_registro(item: Dict) -> Dict:

    titulo = (
        item.get("objetoCompra")
        or item.get("titulo")
        or ""
    )

    orgao = (
        item.get("orgaoEntidade", {})
        .get("razaoSocial", "")
    )

    municipio = (
        item.get("unidadeOrgao", {})
        .get("municipioNome", "")
    )

    uf = (
        item.get("unidadeOrgao", {})
        .get("ufSigla", "")
    )

    modalidade = (
        item.get("modalidadeNome", "")
    )

    publicacao = (
        item.get("dataPublicacaoPncp", "")
    )

    encerramento = (
        item.get("dataEncerramentoProposta", "")
    )

    numero = (
        item.get("numeroControlePNCP", "")
    )

    link = ""

    if numero:

        link = (
            f"https://pncp.gov.br/app/editais/{numero}"
        )

    return {
        "titulo": titulo,
        "orgao": orgao,
        "municipio": municipio,
        "uf": uf,
        "modalidade": modalidade,
        "data_publicacao": fmt_data(publicacao),
        "encerramento": fmt_data(encerramento),
        "link": link
    }


# =========================================================
# SESSION
# =========================================================

if "municipios" not in st.session_state:
    st.session_state.municipios = []

if "resultado" not in st.session_state:
    st.session_state.resultado = None


# =========================================================
# MAIN
# =========================================================

def main():

    st.title("📑 Acerte Licitações")

    st.sidebar.header("Filtros")

    municipios_df = load_municipios()

    palavra = st.sidebar.text_input(
        "Palavra-chave"
    )

    status_label = st.sidebar.selectbox(
        "Status",
        list(STATUS_MAP.keys())
    )

    ufs = sorted(
        municipios_df["uf"]
        .dropna()
        .unique()
        .tolist()
    )

    uf = st.sidebar.selectbox(
        "UF",
        ["Selecione"] + ufs
    )

    if uf != "Selecione":

        mun_df = municipios_df[
            municipios_df["uf"] == uf
        ]

        labels = sorted(
            mun_df["nome"].tolist()
        )

        municipio_nome = st.sidebar.selectbox(
            "Município",
            ["Selecione"] + labels
        )

        if st.sidebar.button("Adicionar"):

            if municipio_nome != "Selecione":

                row = mun_df[
                    mun_df["nome"] == municipio_nome
                ].iloc[0]

                codigo = row["codigo"]

                ja = any(
                    m["codigo"] == codigo
                    for m in st.session_state.municipios
                )

                if not ja:

                    st.session_state.municipios.append({
                        "nome": municipio_nome,
                        "codigo": codigo,
                        "uf": uf
                    })

    st.sidebar.markdown("---")

    st.sidebar.markdown("### Municípios")

    remover = None

    for i, m in enumerate(st.session_state.municipios):

        col1, col2 = st.sidebar.columns([5,1])

        with col1:
            st.write(f"{m['nome']} / {m['uf']}")

        with col2:

            if st.button("X", key=f"rm_{i}"):

                remover = i

    if remover is not None:

        st.session_state.municipios.pop(remover)

        st.rerun()

    pesquisar = st.sidebar.button(
        "Pesquisar",
        type="primary",
        use_container_width=True
    )

    if pesquisar:

        registros = []

        status_api = STATUS_MAP[status_label]

        with st.spinner("Consultando PNCP..."):

            for mun in st.session_state.municipios:

                itens = consultar_pncp(
                    mun["codigo"],
                    status_api
                )

                for item in itens:

                    reg = montar_registro(item)

                    registros.append(reg)

        df = pd.DataFrame(registros)

        if not df.empty and palavra:

            mask = (
                df["titulo"]
                .fillna("")
                .str.contains(
                    palavra,
                    case=False,
                    na=False
                )
            )

            df = df[mask]

        st.session_state.resultado = df

    df = st.session_state.resultado

    if df is None:

        st.info("Selecione os filtros e clique em Pesquisar.")

        return

    st.subheader(f"Resultados ({len(df)})")

    if df.empty:

        st.warning("Nenhum resultado encontrado.")

        return

    for _, row in df.iterrows():

        st.markdown("---")

        st.markdown(
            f"## {row['titulo']}"
        )

        st.write(
            f"**Órgão:** {row['orgao']}"
        )

        st.write(
            f"**Cidade:** {row['municipio']} / {row['uf']}"
        )

        st.write(
            f"**Modalidade:** {row['modalidade']}"
        )

        st.write(
            f"**Publicação:** {row['data_publicacao']}"
        )

        st.write(
            f"**Encerramento:** {row['encerramento']}"
        )

        if row["link"]:

            st.link_button(
                "Abrir edital",
                row["link"]
            )

    st.markdown("---")

    xlsx = io.BytesIO()

    with pd.ExcelWriter(
        xlsx,
        engine="openpyxl"
    ) as writer:

        df.to_excel(
            writer,
            index=False
        )

    st.download_button(
        "Baixar XLSX",
        data=xlsx.getvalue(),
        file_name="licitacoes.xlsx",
        mime=(
            "application/vnd.openxmlformats-"
            "officedocument.spreadsheetml.sheet"
        ),
        use_container_width=True
    )


if __name__ == "__main__":

    main()
