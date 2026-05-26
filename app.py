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

# ==========================
# NOVA API PNCP
# ==========================
BASE_API = "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Referer": "https://pncp.gov.br/app/editais",
}

TAM_PAGINA_FIXO = 100

STATUS_LABELS = [
    "A Receber/Recebendo Proposta",
    "Em Julgamento/Propostas Encerradas",
    "Encerradas",
    "Todos",
]

STATUS_MAP = {
    "A Receber/Recebendo Proposta": "RECEBENDO_PROPOSTA",
    "Em Julgamento/Propostas Encerradas": "PROPOSTA_ENCERRADA",
    "Encerradas": "ENCERRADO",
    "Todos": "",
}

UF_PLACEHOLDER = "— Selecione a UF —"

# ==========================
# Utilitários
# ==========================
def _norm(s: str) -> str:
    s = str(s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def _fmt_dt_iso_to_br(dt: str) -> str:
    if not dt:
        return ""
    try:
        ts = pd.to_datetime(dt, errors="coerce", utc=False)
        if pd.isna(ts):
            return ""
        return ts.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return ""


def _primeiro_valor(*args):
    for a in args:
        if a:
            return a
    return ""


def _build_pncp_link(item: Dict) -> str:
    cnpj = str(
        item.get("cnpj")
        or item.get("orgaoEntidade", {}).get("cnpj")
        or ""
    ).strip()

    ano = str(item.get("anoCompra") or "").strip()
    seq = str(item.get("sequencialCompra") or "").strip()

    if len(cnpj) == 14 and ano and seq:
        return f"{ORIGIN}/app/editais/{cnpj}/{ano}/{seq}"

    return ""


def _uid_from_row(row: Dict) -> str:
    base = (
        f"{row.get('Título','')}-"
        f"{row.get('Cidade','')}-"
        f"{row.get('_pub_raw','')}"
    )
    return hashlib.md5(base.encode("utf-8")).hexdigest()


# ==========================
# GitHub API
# ==========================
def _gh_headers() -> Dict[str, str]:
    tok = st.secrets.get("GITHUB_TOKEN")
    return {
        "Authorization": f"token {tok}",
        "Accept": "application/vnd.github+json",
    }


def _gh_cfg_ok() -> bool:
    return bool(
        st.secrets.get("GITHUB_TOKEN")
        and st.secrets.get("GITHUB_REPO")
    )


def _gh_paths(filename: str) -> Tuple[str, str, str]:
    repo = st.secrets["GITHUB_REPO"]
    branch = st.secrets.get("GITHUB_BRANCH", "main")
    based = st.secrets.get("GITHUB_BASEDIR", "data")
    path = f"{based.rstrip('/')}/{filename}"
    return repo, branch, path


def _gh_get_json(filename: str):
    if not _gh_cfg_ok():
        return None, None

    repo, branch, path = _gh_paths(filename)

    url = f"https://api.github.com/repos/{repo}/contents/{path}"

    r = requests.get(
        url,
        params={"ref": branch},
        headers=_gh_headers(),
        timeout=30,
    )

    if r.status_code == 404:
        return None, None

    r.raise_for_status()

    js = r.json()

    try:
        raw = base64.b64decode(js["content"]).decode("utf-8")
        return json.loads(raw), js.get("sha")
    except Exception:
        return None, js.get("sha")


def _gh_put_json(filename: str, payload: dict, sha=None):
    if not _gh_cfg_ok():
        return

    repo, branch, path = _gh_paths(filename)

    url = f"https://api.github.com/repos/{repo}/contents/{path}"

    content_b64 = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")

    data = {
        "message": f"update {path}",
        "content": content_b64,
        "branch": branch,
    }

    if sha:
        data["sha"] = sha

    r = requests.put(
        url,
        headers=_gh_headers(),
        json=data,
        timeout=30,
    )

    r.raise_for_status()


# ==========================
# CSV municípios PNCP
# ==========================
@st.cache_data(show_spinner=False)
def load_municipios_pncp() -> pd.DataFrame:
    for path in CSV_PNCP_PATHS:
        if os.path.exists(path):
            df = pd.read_csv(path, dtype=str)

            cols = {_norm(c): c for c in df.columns}

            col_nome = cols.get("municipio")
            col_codigo = cols.get("id")
            col_uf = cols.get("uf")

            out = pd.DataFrame({
                "nome": df[col_nome].astype(str).str.strip(),
                "codigo_pncp": df[col_codigo].astype(str).str.strip(),
                "uf": df[col_uf].astype(str).str.strip(),
            })

            out["nome_norm"] = out["nome"].map(_norm)

            return out

    raise FileNotFoundError(
        "ListaMunicipiosPNCP.csv não encontrada."
    )


# ==========================
# CONSULTA PNCP CORRIGIDA
# ==========================
def consultar_pncp_por_municipio(
    municipio_id: str,
    status_value: str = "",
    tam_pagina: int = TAM_PAGINA_FIXO,
    delay_s: float = 0.05,
) -> List[Dict]:

    resultados = []
    pagina = 1

    while True:

        params = {
            "codigoModalidadeContratacao": 1,
            "pagina": pagina,
            "tamanhoPagina": tam_pagina,
            "codigoMunicipioIbge": municipio_id,
        }

        if status_value:
            params["status"] = status_value

        try:
            r = requests.get(
                BASE_API,
                params=params,
                headers=HEADERS,
                timeout=30,
            )

            if r.status_code != 200:
                break

            js = r.json()

            itens = js.get("data", [])

            if not itens:
                break

            resultados.extend(itens)

            if len(itens) < tam_pagina:
                break

            pagina += 1

            time.sleep(delay_s)

        except Exception:
            break

    return resultados


# ==========================
# REGISTRO
# ==========================
def montar_registro(item: Dict, municipio_codigo: str) -> Dict:

    pub_raw = (
        item.get("dataPublicacaoPncp")
        or item.get("dataPublicacao")
        or ""
    )

    fim_raw = (
        item.get("dataEncerramentoProposta")
        or ""
    )

    processo = _primeiro_valor(
        item.get("numeroControlePNCP"),
        item.get("numeroCompra"),
    )

    orgao = (
        item.get("orgaoEntidade", {})
        .get("razaoSocial", "")
    )

    cidade = (
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

    objeto = (
        item.get("objetoCompra", "")
    )

    titulo = (
        item.get("objetoCompra", "")
    )

    return {
        "municipio_codigo": municipio_codigo,
        "Cidade": cidade,
        "UF": uf,
        "Título": titulo,
        "Objeto": objeto,
        "Link para o edital": _build_pncp_link(item),
        "Modalidade": modalidade,
        "Tipo": item.get("instrumentoConvocatorioNome", ""),
        "Orgão": orgao,
        "Publicação": _fmt_dt_iso_to_br(pub_raw),
        "Fim do envio de proposta": _fmt_dt_iso_to_br(fim_raw),
        "numero_processo": str(processo or "").strip(),
        "_pub_raw": pub_raw,
    }


# ==========================
# SESSION
# ==========================
def _ensure_session_state():

    if "selected_municipios" not in st.session_state:
        st.session_state.selected_municipios = []

    if "results_df" not in st.session_state:
        st.session_state.results_df = None

    if "card_page" not in st.session_state:
        st.session_state.card_page = 1

    if "page_size_cards" not in st.session_state:
        st.session_state.page_size_cards = 10


# ==========================
# COLETA
# ==========================
@st.cache_data(ttl=900, show_spinner=False)
def coletar_por_assinatura(signature: dict) -> pd.DataFrame:

    registros = []

    codigos = signature.get("municipios", [])
    status_value = signature.get("status", "")

    for codigo in codigos:

        itens = consultar_pncp_por_municipio(
            codigo,
            status_value=status_value,
        )

        for it in itens:
            registros.append(
                montar_registro(it, codigo)
            )

    df = pd.DataFrame(registros)

    q = (signature.get("q") or "").strip()

    if q and not df.empty:

        mask = (
            df["Título"]
            .fillna("")
            .str.contains(q, case=False, na=False)
            |
            df["Objeto"]
            .fillna("")
            .str.contains(q, case=False, na=False)
        )

        df = df[mask].copy()

    try:
        df["_pub_dt"] = pd.to_datetime(
            df["_pub_raw"],
            errors="coerce",
            utc=False,
        )
    except Exception:
        df["_pub_dt"] = pd.NaT

    df.sort_values(
        "_pub_dt",
        ascending=False,
        inplace=True,
    )

    df.reset_index(drop=True, inplace=True)

    return df


# ==========================
# MAIN
# ==========================
def main():

    st.title("📑 Acerte Licitações — O seu Buscador de Editais")

    _ensure_session_state()

    try:
        pncp_df = load_municipios_pncp()
    except Exception as e:
        st.error(str(e))
        st.stop()

    st.sidebar.header("🔎 Filtros")

    palavra = st.sidebar.text_input(
        "Palavra-chave"
    )

    status_label = st.sidebar.radio(
        "Status",
        STATUS_LABELS,
    )

    ufs = sorted(
        pncp_df["uf"]
        .dropna()
        .unique()
        .tolist()
    )

    uf = st.sidebar.selectbox(
        "UF",
        [UF_PLACEHOLDER] + ufs,
    )

    if uf != UF_PLACEHOLDER:

        temp = pncp_df[
            pncp_df["uf"] == uf
        ].copy()

        labels = (
            temp["nome"] + " / " + temp["uf"]
        ).tolist()

        escolhido = st.sidebar.selectbox(
            "Município",
            ["—"] + labels,
        )

        if st.sidebar.button("Adicionar município"):

            if escolhido != "—":

                nome = escolhido.split(" / ")[0]

                row = temp[
                    temp["nome"] == nome
                ].iloc[0]

                codigo = row["codigo_pncp"]

                if codigo not in [
                    m["codigo_pncp"]
                    for m in st.session_state.selected_municipios
                ]:

                    st.session_state.selected_municipios.append({
                        "codigo_pncp": codigo,
                        "nome": nome,
                        "uf": uf,
                    })

    if st.session_state.selected_municipios:

        st.sidebar.markdown("### Selecionados")

        for m in st.session_state.selected_municipios:
            st.sidebar.markdown(
                f"- {m['nome']} / {m['uf']}"
            )

    pesquisar = st.sidebar.button(
        "🔎 Pesquisar",
        type="primary",
    )

    if pesquisar:

        if not st.session_state.selected_municipios:
            st.warning(
                "Selecione ao menos um município."
            )
            st.stop()

        signature = {
            "municipios": [
                m["codigo_pncp"]
                for m in st.session_state.selected_municipios
            ],
            "status": STATUS_MAP.get(status_label, ""),
            "q": palavra.lower(),
        }

        with st.spinner("Consultando PNCP..."):

            df = coletar_por_assinatura(signature)

        st.session_state.results_df = df.to_dict("records")

    if st.session_state.results_df is None:
        st.info(
            "Configure os filtros e clique em Pesquisar."
        )
        return

    df = pd.DataFrame(
        st.session_state.results_df
    )

    st.subheader(f"Resultados ({len(df)})")

    if df.empty:
        st.warning("Nenhum resultado encontrado.")
        return

    for _, row in df.iterrows():

        st.markdown(f"""
        ### {row.get("Título","")}

        **Cidade:** {row.get("Cidade","")} / {row.get("UF","")}

        **Órgão:** {row.get("Orgão","")}

        **Modalidade:** {row.get("Modalidade","")}

        **Publicação:** {row.get("Publicação","")}

        **Objeto:**  
        {row.get("Objeto","")}

        [Abrir edital]({row.get("Link para o edital","")})

        ---
        """)

    # XLSX
    xlsx_buf = io.BytesIO()

    with pd.ExcelWriter(
        xlsx_buf,
        engine="openpyxl"
    ) as wr:

        df.to_excel(
            wr,
            index=False,
            sheet_name="PNCP"
        )

    st.download_button(
        "Baixar XLSX",
        data=xlsx_buf.getvalue(),
        file_name=f"pncp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    main()
