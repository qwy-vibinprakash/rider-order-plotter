"""
Shared config: load config.json / st.secrets, return DB and ES connections.
"""
import json
import os

import streamlit as st

try:
    from elasticsearch import Elasticsearch
    _ES_AVAILABLE = True
except ImportError:
    _ES_AVAILABLE = False

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def load_config():
    try:
        if "database" in st.secrets:
            cfg = {"database": dict(st.secrets["database"])}
            if "elasticsearch" in st.secrets:
                cfg["elasticsearch"] = dict(st.secrets["elasticsearch"])
            if "app_password" in st.secrets:
                cfg["app_password"] = st.secrets["app_password"]
            return cfg
    except Exception:
        pass
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        st.error("config.json not found and no [database] section in st.secrets.")
        st.stop()


@st.cache_resource
def get_db_config():
    return load_config()["database"]


def get_conn():
    import psycopg2
    cfg = get_db_config()
    return psycopg2.connect(**cfg, connect_timeout=10)


@st.cache_resource
def get_es_client():
    """Returns (Elasticsearch client, index) or (None, None) if not configured."""
    if not _ES_AVAILABLE:
        return None, None
    try:
        cfg = load_config().get("elasticsearch")
        if not cfg:
            return None, None
        es = Elasticsearch(
            cfg["host"],
            basic_auth=(cfg.get("username", ""), cfg.get("password", "")),
            verify_certs=cfg.get("verify_certs", False),
            ssl_show_warn=False,
        )
        return es, cfg.get("index", "logstash")
    except Exception:
        return None, None
