"""TD SQL 数据库连接器（MySQL 兼容）"""
import re
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

    def get_source_table_registry(self, table_name: str = "lineage_source_table_registry") -> tuple[set[str], bool]:
        """从注册表读取所有已注册的来源表名。返回 (set, exists)。"""
        if self._conn is None:
            self.connect()
        cur = self._conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
        """, (self.db, table_name))
        exists = cur.fetchone()[0] > 0
        if not exists:
            cur.close()
            return set(), False
        cur.execute(f'SELECT table_name FROM `{table_name}` WHERE table_name IS NOT NULL')
        rows = cur.fetchall()
        cur.close()
        return {r[0].lower() for r in rows}, True

    def get_table_columns(self, table_name: str, schema: str = None) -> list[str]:
        """从 information_schema.columns 读取表的所有列名"""
        if self._conn is None:
            self.connect()
        schema = schema or self.db
        cur = self._conn.cursor()
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
        """, (schema, table_name))
        rows = cur.fetchall()
        cur.close()
        return [r[0].lower() for r in rows]

    def build_temp_table_procedure_map(self) -> dict[str, tuple[str, str]]:
        """扫描全库所有存储过程源码，构建 {temp_table_name: (proc_name, source)}"""
        if self._conn is None:
            self.connect()
        cur = self._conn.cursor()
        cur.execute("""
            SELECT ROUTINE_NAME, ROUTINE_DEFINITION
            FROM information_schema.ROUTINES
            WHERE ROUTINE_SCHEMA = %s
              AND ROUTINE_TYPE IN ('PROCEDURE', 'FUNCTION')
        """, (self.db,))
        rows = cur.fetchall()
        cur.close()

        result: dict[str, tuple[str, str]] = {}
        create_pattern = re.compile(
            r'(?i)CREATE\s+(?:TEMPORARY\s+)?TABLE\s+(\w+)',
        )
        for proc_name, proc_src in rows:
            if not proc_src:
                continue
            for m in create_pattern.finditer(proc_src):
                tmp_name = m.group(1).lower()
                if tmp_name not in result:
                    result[tmp_name] = (proc_name, proc_src)
        return result
