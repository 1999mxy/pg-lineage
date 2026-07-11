"""TD SQL 数据库连接器（MySQL 兼容）"""
from typing import Optional


class TDSqlConnector:
    def __init__(self, host: str, port: int, db: str, user: str, password: str):
        self.host = host
        self.port = port
        self.db = db
        self.user = user
        self.password = password
        self._conn = None

    def connect(self):
        import pymysql
        self._conn = pymysql.connect(
            host=self.host,
            port=self.port,
            database=self.db,
            user=self.user,
            password=self.password,
        )
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def get_procedure_source(self, proc_name: str, schema: str = None) -> str:
        """
        TD SQL（MySQL 兼容）: 从 information_schema.ROUTINES 读取。
        TD SQL 存储过程源码存储在 ROUTINES.ROUTINE_DEFINITION 中。
        """
        if self._conn is None:
            self.connect()

        cur = self._conn.cursor()
        sql = """
            SELECT ROUTINE_DEFINITION
            FROM information_schema.ROUTINES
            WHERE ROUTINE_NAME = %s
              AND ROUTINE_SCHEMA = %s
              AND ROUTINE_TYPE IN ('PROCEDURE', 'FUNCTION')
        """
        schema = schema or self.db
        cur.execute(sql, (proc_name, schema))
        row = cur.fetchone()
        cur.close()

        if not row:
            raise ValueError(f"存储过程 {schema}.{proc_name} 未找到")

        return row[0]

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()
