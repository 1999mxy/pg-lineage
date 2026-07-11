"""PostgreSQL 数据库连接器"""
import re
from typing import Optional


class PostgresConnector:
    def __init__(self, host: str, port: int, db: str, user: str, password: str = ""):
        self.host = host
        self.port = port
        self.db = db
        self.user = user
        self.password = password
        self._conn = None

    def connect(self):
        import psycopg2
        self._conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=self.db,
            user=self.user,
            password=self.password,
        )
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def get_procedure_source(self, proc_name: str, schema: str = "public") -> str:
        """
        获取存储过程源码（PROCEDURE 或 FUNCTION）。
        PostgreSQL 17+ 支持 PROCEDURE，之前版本用 FUNCTION。
        """
        if self._conn is None:
            self.connect()

        cur = self._conn.cursor()

        # 尝试 PROCEDURE（PG 15+）
        cur.execute("""
            SELECT pg_get_functiondef(p.oid)
            FROM pg_proc p
            JOIN pg_namespace n ON p.pronamespace = n.oid
            WHERE p.proname = %s AND n.nspname = %s
              AND p.prokind = 'p'
        """, (proc_name, schema))
        rows = cur.fetchall()
        if rows:
            cur.close()
            return rows[0][0]

        # 尝试 FUNCTION
        cur.execute("""
            SELECT pg_get_functiondef(p.oid)
            FROM pg_proc p
            JOIN pg_namespace n ON p.pronamespace = n.oid
            WHERE p.proname = %s AND n.nspname = %s
        """, (proc_name, schema))
        rows = cur.fetchall()
        cur.close()

        if not rows:
            raise ValueError(f"存储过程 {schema}.{proc_name} 未找到")

        return rows[0][0]

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()

    def get_source_table_registry(self) -> set[str]:
        """
        从 lineage_source_table_registry 表读取所有已注册的来源表名。
        表不存在时返回空集，不抛异常。
        """
        if self._conn is None:
            self.connect()
        cur = self._conn.cursor()
        cur.execute("""
            SELECT table_name FROM lineage_source_table_registry
            WHERE table_name IS NOT NULL
        """)
        rows = cur.fetchall()
        cur.close()
        return {r[0].lower() for r in rows}

    def build_temp_table_procedure_map(self) -> dict[str, tuple[str, str]]:
        """
        扫描全库所有存储过程源码，构建
        { temp_table_name: (procedure_name, procedure_source) }
        映射。仅匹配 CREATE TABLE tmpX（含 TEMP 前缀），
        不匹配 INSERT INTO（填充动作不是创建）。
        """
        if self._conn is None:
            self.connect()
        cur = self._conn.cursor()
        cur.execute("""
            SELECT proname, pg_get_functiondef(p.oid) AS src
            FROM pg_proc p
            JOIN pg_namespace n ON p.pronamespace = n.oid
            WHERE n.nspname = 'public'
              AND p.prokind IN ('p', 'f')
        """)
        rows = cur.fetchall()
        cur.close()

        import re
        result: dict[str, tuple[str, str]] = {}
        # 匹配 CREATE TEMP TABLE tmpN... 或 CREATE TABLE tmpN...
        create_pattern = re.compile(
            r'(?i)CREATE\s+(?:TEMP\s+)?TABLE\s+(tmp\w*)',
        )
        for proc_name, proc_src in rows:
            for m in create_pattern.finditer(proc_src or ''):
                tmp_name = m.group(1).lower()
                if tmp_name not in result:
                    result[tmp_name] = (proc_name, proc_src)
        return result
    def get_table_columns(self, table_name: str, schema: str = "public") -> list[str]:
        """从 information_schema 查询表的所有列名"""
        if self._conn is None:
            self.connect()
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
