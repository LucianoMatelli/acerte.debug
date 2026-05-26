# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import io
import re
import json
import time
import base64
import unicodedata
import hashlib
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==========================
# Configuração de página
# ==========================
st.set_page_config(
    page_title="Acerte Licitações - O seu Buscador de Editais",
    page_icon="📑",
    layout="wide",
)

# ==========================
# Constantes e caminhos
# ==========================
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")

CSV_PNCP_PATHS = [
    os.path.join(DATA_DIR, "ListaMunicipiosPNCP.csv"),
    "ListaMunicipiosPNCP.csv",
]

CSV_IBGE_PATHS = [
    os.path.join(DATA_DIR, "IBGE_Municipios.csv"),
    "IBGE_Municipios.csv",
]

SAVED_SEARCHES_PATH = os.path.join(BASE_DIR, "saved_searches.json")
SAVED_TR_PATH = os.path.join(BASE_DIR, "tr_marks.json")
SAVED_NA_PATH = os.path.join(BASE_DIR, "na_marks.json")

ORIGIN = "https://pncp.gov.br"
BASE_API = "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://pncp.gov.br/app/editais",
    "Origin": "https://pncp.gov.br",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

TAM_PAGINA_FIXO = 100

STATUS_LABELS = [
    "A Receber/Recebendo Proposta",
    "Em Julgamento/Propostas Encerradas",
    "Encerradas",
    "Todos",
]

STATUS_MAP = {
    "A Receber/Recebendo Proposta": "recebendo_proposta",
    "Em Julgamento/Propostas Encerradas": "proposta_encerrada",
    "Encerradas": "encerrado",
    "Todos": "",
}

UF_PLACEHOLDER = "— Selecione a UF —"

# ==========================
# Sessão HTTP resiliente
# ==========================
def build_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update(HEADERS)

    return session

SESSION = build_session()

# ==========================
# Utilitários
# ==========================
def _norm(s: str) -> str:
    s = str(s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def _items_from_json(js) -> List[Dict]:
    if isinstance(js, dict):
        for key in [
            "data",
            "items",
            "resultados",
            "content",
            "results",
            "licitacoes",
        ]:
            val = js.get(key)
            if isinstance(val, list):
                return val

    if isinstance(js, list):
        return js

    return []


def _fmt_dt_iso_to_br(dt: str) -> str:
    if not dt:
        return ""

    try:
        ts = pd.to_datetime(dt, errors="coerce")
        if pd.isna(ts):
            return ""
        return ts.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return ""


def _full_url(item_url: str) -> str:
    if not item_url:
        return ""

    if isinstance(item_url, str) and item_url.startswith("http"):
        return item_url

    return ORIGIN.rstrip("/") + "/" + str(item_url).lstrip("/")


def _build_pncp_link(item: Dict) -> str:
    cnpj = str(item.get("cnpj") or item.get("orgaoEntidade", {}).get("cnpj") or "").strip()
    ano = str(item.get("anoCompra") or item.get("ano") or "").strip()
    seq = str(item.get("sequencialCompra") or item.get("numero_sequencial") or "").strip()

    if len(cnpj) == 14 and ano and seq:
        return f"{ORIGIN}/app/editais/{cnpj}/{ano}/{seq}"

    return ""


def _primeiro_valor(*args):
    for a in args:
        if a:
            return a
    return ""


def _uid_from_row(row: Dict) -> str:
    base = f"{row.get('Título','')}-{row.get('Orgão','')}-{row.get('Publicação','')}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()

# ==========================
# Loaders
# ==========================
@st.cache_data(show_spinner=False)
def load_municipios_pncp() -> pd.DataFrame:
    encodings = ["utf-8", "utf-8-sig", "latin1", "cp1252"]
    seps = [",", ";", "\t", "|"]

    for path in CSV_PNCP_PATHS:
        if os.path.exists(path):
            for enc in encodings:
                for sep in seps:
                    try:
                        df = pd.read_csv(
                            path,
                            dtype=str,
                            sep=sep,
                            encoding=enc,
                            engine="python",
                            on_bad_lines="skip",
                        )

                        cols_norm = {_norm(c): c for c in df.columns}

                        col_nome = cols_norm.get("municipio") or cols_norm.get("nome")
                        col_codigo = cols_norm.get("id") or cols_norm.get("codigo")
                        col_uf = cols_norm.get("uf")

                        if not col_nome or not col_codigo:
                            continue

                        out = pd.DataFrame({
                            "nome": df[col_nome].astype(str).str.strip(),
                            "codigo_pncp": df[col_codigo].astype(str).str.strip(),
                        })

                        out["uf"] = df[col_uf].astype(str).str.strip() if col_uf else ""
                        out["nome_norm"] = out["nome"].map(_norm)

                        out = out.drop_duplicates(subset=["codigo_pncp"])

                        return out.reset_index(drop=True)

                    except Exception:
                        continue

    raise FileNotFoundError("ListaMunicipiosPNCP.csv não encontrada")

# ==========================
# CONSULTA PNCP CORRIGIDA
# ==========================
def consultar_pncp_por_municipio(
    municipio_id: str,
    status_value: str = "",
    tam_pagina: int = TAM_PAGINA_FIXO,
    delay_s: float = 0.1,
) -> List[Dict]:

    resultados = []
    pagina = 1

    while True:
        params = {
            "codigoModalidadeContratacao": 8,
            "pagina": pagina,
            "tamanhoPagina": tam_pagina,
            "codigoMunicipioIbge": municipio_id,
        }

        try:
            response = SESSION.get(
                BASE_API,
                params=params,
                timeout=60,
            )

            if response.status_code != 200:
                break

            data = response.json()

            itens = _items_from_json(data)

            if not itens:
                break

            if status_value:
                itens_filtrados = []

                for item in itens:
                    situacao = str(
                        item.get("situacaoCompraNome")
                        or item.get("situacaoCompra")
                        or ""
                    ).lower()

                    if status_value == "recebendo_proposta":
                        if "recebendo" in situacao:
                            itens_filtrados.append(item)

                    elif status_value == "proposta_encerrada":
                        if "encerrada" in situacao or "julgamento" in situacao:
                            itens_filtrados.append(item)

                    elif status_value == "encerrado":
                        if "encerrado" in situacao:
                            itens_filtrados.append(item)

                itens = itens_filtrados

            resultados.extend(itens)

            if len(itens) < tam_pagina:
                break

            pagina += 1
            time.sleep(delay_s)

        except Exception as e:
            st.warning(f"Erro ao consultar PNCP: {e}")
            break

    return resultados

# ==========================
# Montagem do registro
# ==========================
def montar_registro(item: Dict, municipio_codigo: str) -> Dict:

    orgao = item.get("orgaoEntidade") or {}

    pub_raw = item.get("dataPublicacaoPncp") or ""
    fim_raw = item.get("dataEncerramentoProposta") or ""

    return {
        "municipio_codigo": municipio_codigo,
        "Cidade": item.get("municipioNome", ""),
        "UF": item.get("ufSigla", ""),
        "Título": item.get("objetoCompra", ""),
        "Objeto": item.get("informacaoComplementar", ""),
        "Link para o edital": _build_pncp_link(item),
        "Modalidade": item.get("modalidadeNome", ""),
        "Tipo": item.get("modoDisputaNome", ""),
        "Orgão": orgao.get("razaoSocial", ""),
        "Publicação": _fmt_dt_iso_to_br(pub_raw),
        "Fim do envio de proposta": _fmt_dt_iso_to_br(fim_raw),
        "numero_processo": item.get("numeroControlePNCP", ""),
        "_pub_raw": pub_raw,
    }

# ==========================
# Estado
# ==========================
def _ensure_session_state():
    if "selected_municipios" not in st.session_state:
        st.session_state.selected_municipios = []

    if "results_df" not in st.session_state:
        st.session_state.results_df = None

# ==========================
# Coleta agregada
# ==========================
@st.cache_data(ttl=900, show_spinner=False)
def coletar_por_assinatura(signature: dict) -> pd.DataFrame:

    registros = []

    for codigo in signature.get("municipios", []):

        itens = consultar_pncp_por_municipio(
            municipio_id=codigo,
            status_value=signature.get("status", ""),
        )

        for item in itens:
            registros.append(montar_registro(item, codigo))

    df = pd.DataFrame(registros)

    if not df.empty:
        try:
            df["_pub_dt"] = pd.to_datetime(df["_pub_raw"], errors="coerce")
            df.sort_values("_pub_dt", ascending=False, inplace=True)
        except Exception:
            pass

    return df

# ==========================
# Main
# ==========================
def main():

    st.title("📑 Acerte Licitações — O seu Buscador de Editais")

    _ensure_session_state()

    try:
        pncp_df = load_municipios_pncp()
    except Exception as e:
        st.error(str(e))
        return

    ufs = sorted(pncp_df["uf"].dropna().unique())

    st.sidebar.header("🔎 Filtros")

    uf = st.sidebar.selectbox("UF", ufs)

    municipios_df = pncp_df[pncp_df["uf"] == uf]

    municipios = municipios_df["nome"].tolist()

    municipio = st.sidebar.selectbox("Município", municipios)

    status = st.sidebar.selectbox("Status", STATUS_LABELS)

    pesquisar = st.sidebar.button("🔎 Pesquisar", type="primary")

    if pesquisar:

        municipio_row = municipios_df[municipios_df["nome"] == municipio]

        if municipio_row.empty:
            st.error("Município não encontrado")
            return

        codigo = municipio_row.iloc[0]["codigo_pncp"]

        signature = {
            "municipios": [codigo],
            "status": STATUS_MAP.get(status, ""),
        }

        with st.spinner("Consultando PNCP..."):
            df = coletar_por_assinatura(signature)

        st.session_state.results_df = df

    df = st.session_state.results_df

    if df is None:
        st.info("Selecione os filtros e clique em pesquisar")
        return

    if df.empty:
        st.warning("Nenhum resultado encontrado")
        return

    st.success(f"{len(df)} resultados encontrados")

    for _, row in df.iterrows():

        st.markdown(f"""
        ### {row.get('Título','')}

        **Cidade:** {row.get('Cidade','')} / {row.get('UF','')}

        **Órgão:** {row.get('Orgão','')}

        **Publicação:** {row.get('Publicação','')}

        **Objeto:** {row.get('Objeto','')}

        [Abrir edital]({row.get('Link para o edital','')})

        ---
        """)

    xlsx_buffer = io.BytesIO()

    with pd.ExcelWriter(xlsx_buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)

    st.download_button(
        "⬇️ Baixar XLSX",
        data=xlsx_buffer.getvalue(),
        file_name=f"pncp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

if __name__ == "__main__":
    main()
