"""Oracle 数据库连接器"""
import re
from typing import Optional


class OracleConnector:
    def __init__(self, host: str, port: int, db: str, user: str, password: str):
        self.host = host
        self.port = port
        self.db = db          # service name
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

    def get_source_table_registry(self, table_name: str = "lineage_source_table_registry") -> tuple[set[str], bool]:
        """从注册表读取所有已注册的来源表名。返回 (set, exists)。"""
        if self._conn is None:
            self.connect()
        cur = self._conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM user_tables WHERE table_name = UPPER(:t)
        """, {"t": table_name})
        exists = cur.fetchone()[0] > 0
        if not exists:
            cur.close()
            return set(), False
        cur.execute(f'SELECT table_name FROM "{table_name}" WHERE table_name IS NOT NULL')
        rows = cur.fetchall()
        cur.close()
        return {r[0].lower() for r in rows}, True

    def get_table_columns(self, table_name: str, schema: str = None) -> list[str]:
        """从 user_tab_columns 读取表的所有列名"""
        if self._conn is None:
            self.connect()
        schema = schema or self.user.upper()
        cur = self._conn.cursor()
        cur.execute("""
            SELECT column_name
            FROM user_tab_columns
            WHERE table_name = UPPER(:tbl) AND owner = UPPER(:own)
            ORDER BY column_id
        """, {"tbl": table_name, "own": schema})
        rows = cur.fetchall()
        cur.close()
        return [r[0].lower() for r in rows]

    def build_temp_table_procedure_map(self) -> dict[str, tuple[str, str]]:
        """扫描全库所有存储过程源码，构建 {temp_table_name: (proc_name, source)}"""
        if self._conn is None:
            self.connect()
        cur = self._conn.cursor()
        cur.execute("""
            SELECT name, text
            FROM user_source
            WHERE type IN ('PROCEDURE', 'FUNCTION')
        """)
        rows = cur.fetchall()
        cur.close()

        result: dict[str, tuple[str, str]] = {}
        create_pattern = re.compile(
            r'(?i)CREATE\s+(?:GLOBAL\s+TEMPORARY\s+|PRIVATE\s+)?TABLE\s+(\w+)',
        )
        seen_procs: dict[str, str] = {}  # name -> full_source
        for proc_name, proc_src in rows:
            if not proc_src or proc_name.upper() in seen_procs:
                continue
            seen_procs[proc_name.upper()] = proc_src
            for m in create_pattern.finditer(proc_src):
                tmp_name = m.group(1).lower()
                if tmp_name not in result:
                    result[tmp_name] = (proc_name.upper(), proc_src)
        return result
