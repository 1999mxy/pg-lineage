"""Oracle 数据库连接器"""
from typing import Optional


class OracleConnector:
    def __init__(self, host: str, port: int, db: str, user: str, password: str):
        self.host = host
        self.port = port
        self.db = db
        self.user = user
        self.password = password
        self._conn = None

    def connect(self):
        import oracledb
        dsn = oracledb.makedsn(self.host, self.port, self.db)
        self._conn = oracledb.connect(user=self.user, password=self.password, dsn=dsn)
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def get_procedure_source(self, proc_name: str, schema: str = None) -> str:
        """Oracle: 从 USER_SOURCE 读取存储过程源码"""
        if self._conn is None:
            self.connect()

        schema = schema or self.user.upper()
        cur = self._conn.cursor()
        cur.execute("""
            SELECT text
            FROM user_source
            WHERE name = :proc_name AND type IN ('PROCEDURE', 'FUNCTION')
            ORDER BY line
        """, {"proc_name": proc_name.upper()})

        rows = cur.fetchall()
        cur.close()

        if not rows:
            raise ValueError(f"存储过程 {schema}.{proc_name} 未找到")

        return "".join(r[0] for r in rows)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()
