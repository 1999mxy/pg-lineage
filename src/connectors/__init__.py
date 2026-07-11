"""数据库连接器，按 db_type 路由"""
from src.connectors.postgres_conn import PostgresConnector
from src.connectors.oracle_conn import OracleConnector
from src.connectors.tdsql_conn import TDSqlConnector

_CONNECTOR_MAP = {
    "postgresql": PostgresConnector,
    "oracle": OracleConnector,
    "tdsql": TDSqlConnector,
}

def get_connector(db_type: str, **kwargs):
    cls = _CONNECTOR_MAP.get(db_type.lower())
    if cls is None:
        raise ValueError(f"不支持的 db_type: {db_type}，支持的类型: {list(_CONNECTOR_MAP.keys())}")
    return cls(**kwargs)
