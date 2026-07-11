"""
ProcedureLineageParser: 存储过程字段级血缘分析器
修复历史:
  Fix 1:  _make_record 输出 src_columns 供外部使用
  Fix 2:  _substitute_expr 递归回溯完整 expression 链，短名优先+从后往前替换
  Fix 5:  INSERT 模式 source_table_names 补充 scalar subquery 中的 tmp 表
"""

from __future__ import annotations
import re
import sqlglot
from typing import Optional
import sys


# ---------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------

class ColumnSource:
    def __init__(
        self,
        src_table: str,
        src_column: str,
        direct: bool = True,
        expression: str = "",
        is_tmp: bool = False,
    ):
        self.src_table = src_table
        self.src_column = src_column
        self.direct = direct
        self.expression = expression
        self.src_columns: list[tuple[str, str]] = []  # [(来源表, 源列名), ...]
        self.is_tmp = is_tmp


class TempTableSchema:
    # Fix 5: 记录该临时表引用的所有 tmp 表名（包括 scalar subquery 中的 tmp）
    def __init__(self, name: str):
        self.name = name
        self.columns: dict[str, ColumnSource] = {}
        self.source_table_names: set[str] = set()
        self._referenced_tmp_names: set[str] = set()

    def add_referenced_tmp(self, name: str):
        self._referenced_tmp_names.add(name.lower())

    def get_referenced_tmp_names(self) -> set[str]:
        return self._referenced_tmp_names

    def add_column(self, col_name: str, source: ColumnSource):
        self.columns[col_name.lower()] = source

    def get_column(self, col_name: str) -> Optional[ColumnSource]:
        return self.columns.get(col_name.lower()) if self.columns else None

    def get_all_columns(self) -> dict[str, ColumnSource]:
        return self.columns


# ---------------------------------------------------------------
# 临时表识别
# ---------------------------------------------------------------

def _is_temp_table(name: str) -> bool:
    """
    判断表名是否为临时表。
    规则：
      - 不以 src_ 开头（源表）
      - 不以 result 开头（结果表）
      - 以 tmp 开头（支持 tmp1_cust_addr、tmp_combined_orders 等）
    """
    if not name:
        return False
    return not name.startswith('src_') and not name.startswith('result') and name.startswith('tmp')


# ---------------------------------------------------------------
# Fix 2: 表达式 TMP 前缀替换工具（短名优先 + 从后往前 + 循环直到无变化）
# ---------------------------------------------------------------

# P0-2: 缓存 tmp_tables 列表，避免每次调用都遍历 schema_map 排序
_SUBSTITUTE_CACHE: dict[int, list] = {}


def _substitute_expr(
    expr: str,
    schema_map: dict,
    depth: int = 0,
) -> str:
    """
    将表达式中所有 tmp.col 引用替换为最上层 src_ 表名.列名。

    P0-2 修复:
      - 按表名长度升序排列（先短后长），避免长名替换切断短名匹配路径
      - 从后往前应用替换，避免字符串位移导致匹配位置错乱
      - 循环直到无变化（max_iter=20），彻底替换所有 tmp 前缀
      - 追溯结果仍为 tmp 时跳过，说明该 tmp 不在 schema_map 中
      - tmp_tables 缓存到 schema_map 的 id()，避免重复排序
    """
    if not expr or depth > 20:
        return expr

    # P0-2: 缓存 tmp_tables，按 schema_map id 复用
    cache_key = id(schema_map)
    if cache_key not in _SUBSTITUTE_CACHE:
        _SUBSTITUTE_CACHE.clear()
        _SUBSTITUTE_CACHE[cache_key] = sorted(
            [t for t in schema_map.keys() if _is_temp_table(t)],
            key=len
        )
    tmp_tables = _SUBSTITUTE_CACHE[cache_key]

    result = expr
    changed = True
    max_iter = 20

    while changed and max_iter > 0:
        changed = False
        max_iter -= 1
        new_result = result

        for tmp_name in tmp_tables:
            pattern = re.compile(
                r'\b(' + re.escape(tmp_name) + r')\.(\w+)\b',
                re.IGNORECASE
            )
            # 从后往前收集所有匹配位置（避免替换后位移问题）
            matches = []
            for m in pattern.finditer(new_result):
                t_name = m.group(1).lower()
                c_name = m.group(2).lower()
                root_t, root_c, _, _ = _trace_column(schema_map, t_name, c_name, 0, set())
                # 只有追溯到非 tmp 表才替换
                if root_t and not _is_temp_table(root_t):
                    matches.append((m.start(), m.end(), f"{root_t}.{root_c}"))

            # 从后往前应用替换
            for start, end, replacement in reversed(matches):
                if new_result[start:end] == replacement:
                    continue
                new_result = new_result[:start] + replacement + new_result[end:]
                changed = True

        result = new_result

    return result


# ---------------------------------------------------------------
# 1. 提取过程体（去除 $$ 定界符）
# ---------------------------------------------------------------

def extract_body(source: str) -> str:
    """提取 PostgreSQL 存储过程体，去除 $$ 定界符"""
    m = re.search(r'\$([a-zA-Z_]*)\$(.+?)\$\1\$', source, re.DOTALL)
    if m:
        return m.group(2)
    m = re.search(r'\$\$(.+?)\$\$', source, re.DOTALL)
    if m:
        return m.group(1)
    return source


# ---------------------------------------------------------------
# 2. 语句切分（按分号，支持括号嵌套）
# ---------------------------------------------------------------

def split_by_semicolon(text: str) -> list[str]:
    """按分号切分语句，忽略字符串和括号内的分号"""
    statements = []
    depth = 0
    current = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in ("'", '"'):
            quote = ch
            current += ch
            i += 1
            while i < len(text):
                if text[i] == quote and (i == 0 or text[i-1] != '\\'):
                    current += text[i]
                    i += 1
                    break
                current += text[i]
                i += 1
            continue
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ';' and depth == 0:
            if current.strip():
                statements.append(current.strip())
            current = ""
        else:
            current += ch
        i += 1
    if current.strip():
        statements.append(current.strip())
    return statements


# ---------------------------------------------------------------
# 3. 从 SELECT 语句中提取列名列表
# ---------------------------------------------------------------

def extract_select_columns(select_sql: str, dialect: str = "postgres") -> list[str]:
    """从 SELECT 语句中提取 SELECT 列表的列名（支持别名）"""
    cols = []
    sql = re.sub(r"(?i)^\s*SELECT\s+", "", select_sql.strip(), count=1)
    from_m = re.search(r"(?i)\bFROM\b", sql)
    if from_m:
        sql = sql[:from_m.start()]
    depth = 0
    buf = []
    for ch in sql:
        if ch == '(':
            depth += 1
            buf.append(ch)
        elif ch == ')':
            depth -= 1
            buf.append(ch)
        elif ch == ',' and depth == 0:
            buf.append('\x00SEP\x00')
        else:
            buf.append(ch)
    sql = "".join(buf)
    for part in sql.split('\x00SEP\x00'):
        part = part.strip()
        if not part or re.match(r"(?i)^\s*(DISTINCT|ALL)\s*$", part):
            continue
        part = re.sub(r"(?i)\s+AS\s+", " AS ", part)
        m = re.search(r"(?i)\s+AS\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", part)
        if m:
            cols.append(m.group(1).lower())
            continue
        func_m = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*\([^*]*\*[^*]*\)\s*$", part)
        if func_m:
            cols.append(func_m.group(1).lower())
            continue
        alias_m = re.search(r"\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", part)
        if alias_m:
            cols.append(alias_m.group(1).lower())
            continue
        words = part.split()
        last_word = words[-1] if words else part
        if "." in last_word:
            col_name = last_word.rsplit(".", 1)[-1].strip()
        else:
            col_name = last_word.strip()
        col_name = re.sub(r"\(.*", "", col_name).strip()
        if col_name:
            cols.append(col_name.lower())

    # P0-3: 正则解析失败时（复杂嵌套表达式），fallback 到 sqlglot
    if not cols and select_sql.strip():
        try:
            import sqlglot
            ast = sqlglot.parse_one(select_sql, dialect=dialect)
            select_node = ast.find(sqlglot.exp.Select)
            if select_node:
                for node in select_node.expressions:
                    col_name = ""
                    if isinstance(node, sqlglot.exp.Alias):
                        col_name = node.alias
                    elif isinstance(node, sqlglot.exp.Column):
                        col_name = node.name
                    elif isinstance(node, sqlglot.exp.Func):
                        col_name = node.sql_name().lower()
                    else:
                        col_name = str(node).split(" AS ")[-1].strip()
                    col_name = col_name.lower()
                    if col_name and col_name not in cols:
                        cols.append(col_name)
        except Exception:
            pass

    return cols


# ---------------------------------------------------------------
# 4. 语句解析（识别 DDL 类型）
# ---------------------------------------------------------------


# ---------------------------------------------------------------
# 动态 SQL 解析（方案 B：数据库元数据回查）
# ---------------------------------------------------------------

def _extract_execute_sql(raw):
    raw_stripped = raw.strip()
    # EXECUTE format("...", ...)
    fm = re.search(
        r'(?i)\bEXECUTE\s+format\s*\(\s*([\x27\x22])(.+?)\1(?:\s*,|\s*\))',
        raw_stripped, re.DOTALL
    )
    if fm:
        return fm.group(2)
    # EXECUTE '...' 或 EXECUTE "..."
    fm = re.search(
        r"(?i)\bEXECUTE\s+([\x27\x22])(.+?)\1(?:\s+USING|\s+INTO|\s*;|\s*$)",
        raw_stripped, re.DOTALL
    )
    if fm:
        sql = fm.group(2)
        ui = re.search(r"(?i)\bUSING\b", sql)
        if ui:
            sql = sql[:ui.start()]
        return sql.strip()
    return None


def _extract_src_table_from_sql(sql):
    upper = sql.upper()
    depth, fp, i = 0, -1, 0
    while i < len(sql) - 1:
        i += 1
        ch = sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch in ("\x27", "\x22"):
            q, j = ch, i + 1
            while j < len(sql) and not (sql[j] == q and sql[j-1] != "\\"):
                j += 1
            i = j
        elif depth == 0 and upper.startswith("FROM", i):
            fp = i + 4
            break
    if fp < 0:
        return None
    rest = sql[fp:].lstrip()
    if rest.startswith("("):
        return None
    m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", rest)
    return m.group(1).lower() if m else None


def split_statements(body: str, dialect: str = "postgres") -> list[dict]:
    """
    将存储过程体拆成独立语句。
    body 应为 extract_body() 提取后的纯净过程体。
    dialect 用于 INSERT INTO 分支预提取 FROM 表名。
    """
    statements = []
    raw_list = split_by_semicolon(body)
    for raw in raw_list:
        raw = raw.strip()
        if not raw:
            continue
        # CREATE [TEMP] TABLE xxx AS SELECT ...
        m = re.search(
            r"(?i)\bCREATE\s+(?:TEMP\s+)?TABLE\s+(\S+)\s+AS\s+SELECT\s*",
            raw,
        )
        if m:
            tbl = m.group(1).strip().strip('"').strip().lower()
            sel = raw[m.end():].strip()
            cols = extract_select_columns("SELECT " + sel, dialect)
            statements.append({
                "type": "create_as",
                "table": tbl,
                "select": sel,
                "cols": cols,
            })
            continue
        # CREATE [TEMP] TABLE xxx (col_list)
        m = re.search(
            r"(?i)\bCREATE\s+(?:TEMP\s+)?TABLE\s+(\S+)",
            raw,
        )
        if m:
            tbl = m.group(1).strip().strip('"').strip().lower()
            rest = raw[m.end():].strip()
            if rest.startswith("("):
                depth, end_idx = 0, 0
                for i, ch in enumerate(rest):
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            end_idx = i
                            break
                col_str = rest[1:end_idx]
                cols = [
                    c.strip().split()[0].strip().strip('"').lower()
                    for c in col_str.split(",") if c.strip()
                ]
                statements.append({
                    "type": "create_def",
                    "table": tbl,
                    "cols": cols,
                })
                continue
        # INSERT INTO xxx SELECT ...
        m = re.search(r"(?i)\bINSERT\s+INTO\s+(\S+)\s+SELECT", raw)
        if m:
            tbl = m.group(1).strip().strip('"').strip().lower()
            sel = raw[m.end():].strip()
            cols = extract_select_columns("SELECT " + sel, dialect)
            # P1-7: 预提取 INSERT INTO SELECT 中的 FROM 表名
            _, _, src_tables = _build_select_expanded({}, sel, tbl, dialect)
            statements.append({
                "type": "insert_into",
                "table": tbl,
                "select": sel,
                "cols": cols,
                "source_table_names": src_tables,
            })
            continue
        # EXECUTE 动态 SQL —— 方案 B：提取 FROM 表名，字段血缘依赖 metadata 回查
        exe_m = re.search(r"(?i)EXECUTE", raw)
        if exe_m:
            exec_sql = _extract_execute_sql(raw)
            if exec_sql:
                src_tbl = _extract_src_table_from_sql(exec_sql)
                exec_select_cols = extract_select_columns("SELECT " + exec_sql, dialect)
                statements.append({
                    "type": "execute_dynamic",
                    "raw_sql": exec_sql,
                    "src_table": src_tbl,
                    "cols": exec_select_cols,
                    "fallback_to_metadata": True,
                })
            else:
                statements.append({"type": "execute_unknown", "raw": raw})
            continue

    return statements


# ---------------------------------------------------------------
# 5. 解析单个 SELECT（使用 sqlglot）
# ---------------------------------------------------------------

def _is_derived_expression(expr_text: str) -> bool:
    """判断表达式是否为派生（含有函数/运算符）"""
    if not expr_text:
        return False
    low = expr_text.upper()
    ops = ["CASE", "WHEN", "COALESCE", "NULLIF", "GREATEST", "LEAST",
           "ROUND", "TRUNC", "ABS", "CAST", "CONCAT", "SUBSTR", "SUBSTRING",
           "+", "-", "*", "/", "||", "::", "COUNT", "SUM", "AVG", "MAX", "MIN",
           "NVL", "IFNULL", "NVL2", "DECODE", "POWER", "SQRT",
           "LOG", "LN", "EXP", "FLOOR", "CEIL", "CEILING",
           "TO_CHAR", "TO_DATE", "TO_NUMBER", "EXTRACT", "DATE_TRUNC",
           "DENSE_RANK", "RANK", "ROW_NUMBER", "LAG", "LEAD"]
    return any(op in low for op in ops)


def parse_select(select_sql: str, dialect: str = "postgres") -> dict:
    """
    解析单个 SELECT SQL，返回字段来源信息。
    select_sql 可以是 "SELECT ..." 或 "col1, col2 FROM ..." 格式。
    返回格式: { col_name: { src_table, src_column, is_derived, src_columns, expression } }
    """
    result = {}
    try:
        ast = sqlglot.parse_one(select_sql, dialect=dialect)
    except Exception:
        return result
    select_node = ast.find(sqlglot.exp.Select)
    if not select_node:
        return result
    for expr in select_node.expressions:
        is_derived = False
        src_columns: list[tuple[str, str]] = []
        out_col_name = None
        actual_expr = expr
        if isinstance(expr, sqlglot.exp.Alias):
            out_col_name = expr.alias.lower() if expr.alias else None
            actual_expr = expr.this
        for node in actual_expr.walk():
            if isinstance(node, sqlglot.exp.Column):
                tbl = (node.table or "").lower()
                col_name = node.name.lower()
                if tbl and col_name:
                    src_columns.append((tbl, col_name))
        if isinstance(actual_expr, (sqlglot.exp.Alias, sqlglot.exp.Case, sqlglot.exp.If,
                                     sqlglot.exp.Coalesce, sqlglot.exp.Cast,
                                     sqlglot.exp.Binary, sqlglot.exp.Unary,
                                     sqlglot.exp.AggFunc, sqlglot.exp.Anonymous)):
            is_derived = True
        if _is_derived_expression(actual_expr.sql(dialect=dialect)):
            is_derived = True
        if len({t for t, c in src_columns}) > 1:
            is_derived = True
        primary_src_table = ""
        primary_src_col = ""
        if src_columns:
            for (st, sc) in src_columns:
                if _is_temp_table(st):
                    primary_src_table = st
                    primary_src_col = sc
                    break
            if not primary_src_table:
                primary_src_table, primary_src_col = src_columns[0]
        if out_col_name is not None:
            col_name = out_col_name
        elif isinstance(actual_expr, sqlglot.exp.Column):
            col_name = (actual_expr.this.name or '').lower()
        else:
            col_name = None
        if col_name:
            result[col_name] = {
                "src_table": primary_src_table,
                "src_column": primary_src_col,
                "is_derived": is_derived,
                "src_columns": src_columns,
                "expression": actual_expr.sql(dialect=dialect),
            }
    return result


# ---------------------------------------------------------------
# 6. 子查询别名展开
# ---------------------------------------------------------------

def _resolve_subquery_aliases_in_select(select_sql: str, dialect: str) -> dict[str, str]:
    """
    从 SELECT SQL 中提取子查询别名 -> 真实表名的映射。
    处理形如: JOIN (SELECT ... FROM src_table) alias ON ...
    """
    alias_map: dict[str, str] = {}
    clean = re.sub(r"--.*$", "", select_sql, flags=re.MULTILINE)
    pos = 0
    while pos < len(clean):
        join_m = re.search(r"(?i)\bJOIN\s+\(", clean[pos:])
        if not join_m:
            break
        join_start = pos + join_m.end()
        depth, subq_end = 1, join_start
        while subq_end < len(clean) and depth > 0:
            ch = clean[subq_end]
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            subq_end += 1
        subq_body = clean[join_start:subq_end - 1]
        rest_after = clean[subq_end:].lstrip()
        alias_m = re.match(r"(?i)(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*)", rest_after)
        if alias_m:
            alias = alias_m.group(1).lower()
        else:
            pos += 1
            continue
        inner_from = re.search(r"(?i)\bFROM\b\s+([A-Za-z_][A-Za-z0-9_]*)", subq_body)
        if inner_from:
            real_table = inner_from.group(1).lower()
            alias_map[alias] = real_table
        pos = subq_end
    return alias_map


# ---------------------------------------------------------------
# 7. 构建 Schema Map（正向）—— sqlglot AST 精准提取表别名
# ---------------------------------------------------------------

def _build_select_expanded(
    schema_map: dict, select_sql: str, target_table: str, dialect: str
) -> tuple[dict, str, set[str]]:
    """
    用 sqlglot AST 精准提取 SELECT 中的表名、别名，并展开列引用。
    返回 (alias_map, expanded_sql, all_source_tables)。
    """
    import re as re2

    clean = re2.sub(r"--.*$", "", select_sql, flags=re2.MULTILINE)

    if not clean.lstrip().upper().startswith("SELECT"):
        clean = "SELECT " + clean.lstrip()

    alias_map: dict[str, str] = {}
    all_source_tables: set[str] = set()

    try:
        ast = sqlglot.parse_one(clean, dialect=dialect)
    except Exception:
        return {}, clean, set()

    select_node = ast.find(sqlglot.exp.Select)
    if not select_node:
        return {}, clean, set()

    for node in select_node.walk():
        if isinstance(node, sqlglot.exp.Table):
            tbl_name = node.name.lower()
            explicit_alias = (node.alias or "").lower().strip()
            all_source_tables.add(tbl_name)
            if explicit_alias and explicit_alias != tbl_name:
                alias_map[explicit_alias] = tbl_name

    subq_aliases = _resolve_subquery_aliases_in_select(clean, dialect)
    for alias, real_table in subq_aliases.items():
        alias_map[alias] = real_table
        all_source_tables.add(real_table)

    expanded = clean
    for alias, real_table in alias_map.items():
        expanded = re2.sub(
            r'\b(' + re2.escape(alias) + r')\.(\w+)',
            real_table + r'.\2',
            expanded,
            flags=re2.IGNORECASE
        )

    upper_expanded = expanded.lstrip().upper()
    if upper_expanded.startswith("SELECT"):
        sel_end = len("SELECT")
        while sel_end < len(expanded) and expanded[sel_end].isspace():
            sel_end += 1
        expanded = expanded[sel_end:].lstrip()

    if target_table in schema_map:
        schema_map[target_table].source_table_names.update(all_source_tables)

    return alias_map, expanded, all_source_tables


def _extract_referenced_tmp_tables(schema_map: dict, select_sql: str) -> set[str]:
    """从 SELECT SQL 中提取所有引用的 tmp 表名（FROM/JOIN 子句）"""
    referenced: set[str] = set()
    _, _, all_src = _build_select_expanded(schema_map, select_sql, "", "postgres")
    for tbl in all_src:
        if _is_temp_table(tbl):
            referenced.add(tbl)
    return referenced


# ---------------------------------------------------------------
# 8. 构建 Schema Map（正向）
# ---------------------------------------------------------------

def build_schema_map(stmts: list[dict], dialect: str = "postgres", conn=None) -> dict[str, TempTableSchema]:
    """遍历所有语句，构建每个临时表/结果表的字段血缘 schema_map。"""
    schema_map: dict[str, TempTableSchema] = {}

    def _ensure_table(name: str):
        name = name.lower()
        if name not in schema_map:
            schema_map[name] = TempTableSchema(name)
        return schema_map[name]

    def _detect_from_table(select_sql: str) -> str:
        fm = re.search(r"(?i)\bFROM\s+([A-Za-z_][\w0-9_]*)", select_sql)
        if fm:
            return fm.group(1).lower()
        return ""

    def _process_select_stmt(tbl_name: str, select_sql: str, explicit_cols: list, pre_extracted_src: set = None):
        """处理 CREATE AS 或 INSERT INTO SELECT 的共用逻辑"""
        _ensure_table(tbl_name)
        _table_alias_map, expanded_sql, all_src = _build_select_expanded(
            schema_map, select_sql, tbl_name, dialect
        )
        schema_map[tbl_name].source_table_names.update(all_src)
        if pre_extracted_src:
            schema_map[tbl_name].source_table_names.update(pre_extracted_src)

        # Fix 5: 补充 scalar subquery 中的 tmp 表
        referenced_tmp = _extract_referenced_tmp_tables(schema_map, select_sql)
        for rt in referenced_tmp:
            if rt not in schema_map[tbl_name].source_table_names:
                schema_map[tbl_name].source_table_names.add(rt)
            schema_map[tbl_name].add_referenced_tmp(rt)

        col_map = parse_select("SELECT " + expanded_sql, dialect)

        select_cols = explicit_cols or list(col_map.keys())
        star_sources = {}
        cols_to_process = []

        # ------------------------------------------------------------
        # SELECT * 继承逻辑增强：对 FROM/JOIN 中所有表都继承其列，
        # 不只第一个表。修复 SELECT * FROM tmp2_order_detail 的场景。
        # ------------------------------------------------------------
        _all_from_tables = []
        _detect_m = re.search(
            r"(?i)\bFROM\s+(.*?)(?=\s+(?:LEFT|RIGHT|INNER|OUTER|JOIN|WHERE|GROUP|ORDER|LIMIT|UNION)|$)",
            "SELECT " + expanded_sql
        )
        if _detect_m:
            _from_body = _detect_m.group(1).strip()
            _table_refs = re.findall(
                r"(?i)(?:JOIN\s+)?(?:(?:LEFT|RIGHT|INNER|OUTER|CROSS)?\s*JOIN\s+)?(?:\(\s*SELECT\s+[^)]+\)\s+AS\s+|)(\w+)",
                _from_body
            )
            _all_from_tables = [t.lower() for t in _table_refs
                                 if t.lower() not in ("left", "right", "inner", "outer", "cross", "join")]

        for col in select_cols:
            if col == "*":
                # 从所有 FROM 表继承列（不只是第一个）
                for src_tbl in _all_from_tables:
                    if src_tbl in schema_map:
                        for inherited_col, inherited_src in schema_map[src_tbl].get_all_columns().items():
                            if inherited_col not in cols_to_process:
                                cols_to_process.append(inherited_col)
                                star_sources[inherited_col] = inherited_src
            else:
                if col not in cols_to_process:
                    cols_to_process.append(col)

        for col in cols_to_process:
            key = col.lower()
            entry = col_map.get(key)
            if not entry:
                for v_key, v_entry in col_map.items():
                    if any(sc[1].lower() == key for sc in v_entry["src_columns"]):
                        entry = v_entry
                        break
            if col in star_sources:
                schema_map[tbl_name].add_column(col, star_sources[col])
                continue
            if entry:
                src_table = entry["src_table"]
                src_col = entry["src_column"]
                is_derived = entry["is_derived"]
                src_cols = entry["src_columns"]
                expr_text = entry.get("expression", "")
            else:
                src_table, src_col, is_derived = "", col, False
                src_cols = []
                expr_text = ""
            if not is_derived and src_table and _is_temp_table(src_table):
                if src_table in schema_map:
                    prev = schema_map[src_table].get_column(src_col)
                    if prev and not prev.direct:
                        is_derived = True
                        src_cols = prev.src_columns or [(src_table, src_col)]
            is_tmp = bool(_is_temp_table(src_table)) if src_table else False
            cs = ColumnSource(src_table, src_col, direct=not is_derived, is_tmp=is_tmp)
            cs.src_columns = src_cols
            cs.expression = expr_text
            schema_map[tbl_name].add_column(col, cs)

    for stmt in stmts:
        stmt_type = stmt["type"]
        tbl_name = stmt["table"]
        if stmt_type == "create_as":
            _process_select_stmt(tbl_name, stmt["select"], stmt.get("cols"), stmt.get("source_table_names"))
        elif stmt_type == "create_def":
            _ensure_table(tbl_name)
            for col in stmt.get("cols", []):
                cs = ColumnSource("", "", direct=False)
                schema_map[tbl_name].add_column(col, cs)
        elif stmt_type == "insert_into":
            _process_select_stmt(tbl_name, stmt["select"], stmt.get("cols"), stmt.get("source_table_names"))

        elif stmt_type == "execute_dynamic":
            # 方案 B：动态 SQL，从 SQL 字面提取列名，列不全时用 metadata 回查
            src_tbl = stmt.get("src_table", "")
            raw_cols = stmt.get("cols", [])
            if conn and src_tbl and not raw_cols:
                raw_cols = conn.get_table_columns(src_tbl)
            if src_tbl:
                schema_map.setdefault(tbl_name, TempTableSchema(tbl_name))
                schema_map[tbl_name].source_table_names.add(src_tbl)
                for col in raw_cols:
                    cs = ColumnSource(src_tbl, col, direct=True, is_tmp=False)
                    schema_map[tbl_name].add_column(col, cs)
        elif stmt_type == "execute_unknown":
            pass

    return schema_map


# ---------------------------------------------------------------
# 9. 血缘追溯（逆向）
# ---------------------------------------------------------------

def _trace_column(
    schema_map: dict[str, TempTableSchema],
    src_table: str,
    src_col: str,
    depth: int = 0,
    visited: set = None,
) -> tuple[str, str, list[str], None]:
    """递归追溯单个列至最上层来源表。返回 (root_table, root_col, chain, None)。"""
    if visited is None:
        visited = set()
    visited = set(visited)  # 复制避免污染调用方
    if depth > 30:
        return src_table, src_col, [src_table] if src_table else [], None
    if not src_table:
        return "", "", [], None
    key = (src_table, src_col)
    if key in visited:
        return src_table, src_col, [src_table], None
    visited.add(key)
    if _is_temp_table(src_table) and src_table in schema_map:
        src = schema_map[src_table].get_column(src_col)
        if src and src.src_table:
            next_tbl, next_col = src.src_table, src.src_column
            root_t, root_c, chain_tail, _ = _trace_column(
                schema_map, next_tbl, next_col, depth + 1, visited
            )
            return root_t, root_c, [src_table] + chain_tail, None
        # 回退：列不在当前临时表，从 source_table_names 找其他来源
        # Fix 5: 同时检查 _referenced_tmp_names（scalar subquery 中的 tmp）
        all_candidates = (
            schema_map[src_table].source_table_names |
            schema_map[src_table].get_referenced_tmp_names()
        )
        for cand in sorted(all_candidates):
            if cand == src_table or not _is_temp_table(cand):
                continue
            if cand in schema_map:
                cand_src = schema_map[cand].get_column(src_col)
                if cand_src and cand_src.src_table:
                    root_t, root_c, chain_tail, _ = _trace_column(
                        schema_map, cand_src.src_table, cand_src.src_column, depth + 1, visited
                    )
                    return root_t, root_c, [cand] + chain_tail, None

        # P0-3 完整修复：扫描所有列，找任意指向其他 tmp 表的列，递归追溯后继续回退
        for other_col, other_src in schema_map[src_table].get_all_columns().items():
            if not other_col or not other_src:
                continue
            if not _is_temp_table(other_src.src_table):
                continue
            sub_root_t, sub_root_c, sub_tail, _ = _trace_column(
                schema_map, other_src.src_table, other_src.src_column, depth + 1, visited
            )
            if sub_root_t and not _is_temp_table(sub_root_t):
                return sub_root_t, sub_root_c, [src_table] + sub_tail, None

        return src_table, src_col, [src_table], None
    if src_table in schema_map:
        src = schema_map[src_table].get_column(src_col)
        if src and src.src_table:
            root_t, root_c, chain_tail, _ = _trace_column(
                schema_map, src.src_table, src.src_column, depth + 1, visited
            )
            return root_t, root_c, [src_table] + chain_tail, None
    return src_table, src_col, [src_table], None


def trace_lineage(schema_map: dict[str, TempTableSchema], result_table: str) -> list[dict]:
    lineage = []
    schema = schema_map.get(result_table.lower())
    if not schema:
        return lineage
    for col_name, col_source in schema.get_all_columns().items():
        lineage.extend(_trace_single(schema_map, result_table.lower(), col_name, col_source))
    return lineage


def _is_simple_column_ref(expr: str) -> bool:
    """判断表达式是否只是简单列引用（无运算/函数）"""
    if not expr:
        return False
    stripped = re.sub(r"\b[A-Za-z_]\w*\.?", "", expr).strip()
    return not stripped or re.match(r"^[A-Za-z_]\w*$", stripped)


def _fill_expr_from_chain(schema_map: dict, src_table: str, src_col: str, depth: int = 0) -> str:
    """递归从上游临时表链回溯 expression，直到找到有意义的 expression。"""
    if depth > 30 or not src_table:
        return ""
    if not _is_temp_table(src_table):
        return ""
    if src_table not in schema_map:
        return ""
    upstream = schema_map[src_table].get_column(src_col)
    if not upstream:
        return ""
    e = getattr(upstream, "expression", "") or ""
    if e and not _is_simple_column_ref(e):
        return e
    # Fix 2: 递归追溯上游 expression，不要只回溯一层
    if upstream.src_table:
        deeper = _fill_expr_from_chain(schema_map, upstream.src_table, upstream.src_column, depth + 1)
        if deeper:
            return deeper
    return ""


def _trace_single(
    schema_map: dict[str, TempTableSchema],
    table_name: str,
    col_name: str,
    col_source: ColumnSource,
) -> list[dict]:
    """
    追溯单个字段的血缘。
    Fix 1: 传递 src_columns 到 output（供 mapping_fmt / summary 使用）
    Fix 2: 对 expression 做 tmp 前缀替换（_substitute_expr）
    """
    if not col_source.src_table:
        return [_make_record(col_name, "", "", "direct", [], "", "")]

    # Fix 1: 传递 src_columns 到 output
    src_columns = getattr(col_source, "src_columns", []) or []

    # Fix 2: 对 expression 做 tmp 前缀替换
    raw_expr = getattr(col_source, "expression", "") or ""
    if raw_expr:
        expr = _substitute_expr(raw_expr, schema_map)
    else:
        expr = ""

    # Fix 2: 如果 expression 仍是简单引用，尝试递归回溯上游
    is_meaningful_expr = bool(expr) and not _is_simple_column_ref(expr)
    if not is_meaningful_expr and col_source.src_table:
        upstream_expr = _fill_expr_from_chain(
            schema_map, col_source.src_table, col_source.src_column
        )
        if upstream_expr:
            expr = _substitute_expr(upstream_expr, schema_map)

    if col_source.direct:
        root_table, root_col, chain, _ = _trace_column(
            schema_map, col_source.src_table, col_source.src_column
        )
        # 过滤 chain：去掉末尾非 tmp 表
        filtered = list(chain)
        while filtered and not _is_temp_table(filtered[-1]):
            filtered.pop()
        # 如果链尾还是 tmp 但指向另一个 tmp，继续追溯
        if filtered:
            last_hop = filtered[-1]
            if last_hop in schema_map:
                src = schema_map[last_hop].get_column(col_name)
                if src and src.src_table and _is_temp_table(src.src_table):
                    root_t, root_c, chain_tail, _ = _trace_column(
                        schema_map, src.src_table, src.src_column
                    )
                    filtered = [last_hop] + chain_tail
                    root_table, root_col = root_t, root_c
        chain_str = " \u2192 ".join(filtered) if filtered else ""
        return [_make_record(col_name, root_table, root_col, "direct", src_columns, chain_str, expr)]

    # 派生表达式
    records = []
    for (src_t, src_c) in col_source.src_columns:
        root_table, root_col, chain, _ = _trace_column(schema_map, src_t, src_c)
        filtered = list(chain)
        while filtered and not _is_temp_table(filtered[-1]):
            filtered.pop()
        chain_str = " \u2192 ".join(filtered) if filtered else ""
        records.append(_make_record(col_name, root_table, root_col, "derived", src_columns, chain_str, expr))
    return records


def _make_record(col_name, src_table, src_column, lineage_type, src_columns, chain_str="", expression=""):
    return {
        "result_column": col_name,
        "src_schema": "public",
        "src_table": src_table,
        "src_column": src_column,
        "lineage_type": lineage_type,
        "expression": expression,
        "chain": chain_str,
        "src_columns": src_columns,  # Fix 1: 输出 src_columns 供外部使用
    }


# ---------------------------------------------------------------
# 主解析器
# ---------------------------------------------------------------

class ProcedureLineageParser:
    def __init__(self, db_type: str = "postgresql", dialect: str = "postgres"):
        self.db_type = db_type
        self.dialect = dialect

    def parse(self, proc_name: str, schema: str, conn) -> dict:
        result, _ = self.parse_full(proc_name, schema, conn)
        return result

    def parse_full(self, proc_name: str, schema: str, conn) -> tuple[dict, dict]:
        """返回 (result_dict, schema_map)"""
        raw = conn.get_procedure_source(proc_name, schema)
        body = extract_body(raw)
        stmts = split_statements(body, self.dialect)
        schema_map = build_schema_map(stmts, self.dialect, conn)
        result_tables = [t for t in schema_map if not _is_temp_table(t)]
        result_tables_lineage = []
        for tbl in result_tables:
            cols = schema_map[tbl].get_all_columns()
            lineage = trace_lineage(schema_map, tbl)
            result_tables_lineage.append({
                "table": tbl,
                "columns": len(cols),
                "lineage": lineage,
            })
        result = {
            "procedure": proc_name,
            "db_type": self.db_type,
            "result_tables": result_tables_lineage,
        }
        return result, schema_map

    def validate_lineage(
        self, schema_map: dict, result_tables: list[str], registry: set[str]
    ) -> list[dict]:
        """验证血缘追溯结果：所有来源表必须在 registry 中"""
        warnings = []
        for tbl in result_tables:
            if tbl.lower() not in schema_map:
                continue
            for col_name, col_source in schema_map[tbl.lower()].get_all_columns().items():
                src_t = col_source.src_table or ""
                src_c = col_source.src_column or ""
                if not src_t:
                    continue
                if _is_temp_table(src_t):
                    root_t, root_c, chain, _ = _trace_column(schema_map, src_t, src_c)
                else:
                    root_t, root_c = src_t, src_c
                    chain = [src_t]
                for hop_tbl in chain:
                    if not _is_temp_table(hop_tbl) and hop_tbl.lower() not in registry:
                        warnings.append({
                            "result_table": tbl,
                            "result_column": col_name,
                            "root_table": root_t,
                            "root_column": root_c,
                            "chain": " \u2192 ".join(chain),
                            "reason": f"链路中出现未注册的表 [{hop_tbl}]，说明临时表未被完全穿透",
                        })
                        break
        return warnings


# ---------------------------------------------------------------
# 10. 跨过程血缘追溯
# ---------------------------------------------------------------

def _resolve_cross_procedure(
    tmp_name: str,
    current_proc: str,
    conn,
    visited: set,
    dialect: str,
    tmp_proc_map: dict,
    cached_schemas: dict,
) -> dict[str, TempTableSchema]:
    """解析创建 tmp_name 的存储过程，返回其完整 schema_map。"""
    if tmp_name not in tmp_proc_map:
        return {}
    creating_proc, _ = tmp_proc_map[tmp_name]
    if creating_proc == current_proc:
        return {}
    if creating_proc in visited:
        return {}
    if creating_proc in cached_schemas:
        return cached_schemas[creating_proc]
    visited.add(creating_proc)
    raw = conn.get_procedure_source(creating_proc, "public")
    body = extract_body(raw)
    stmts = split_statements(body, dialect)
    sub_schema_map = build_schema_map(stmts, dialect, conn)
    # 对子过程 schema_map 再次触发跨过程追溯，补全子过程中引用的 tmp 的来源
    _resolve_all_cross_procedures(
        sub_schema_map, creating_proc,
        conn, visited, dialect,
        tmp_proc_map, cached_schemas,
        depth=0,
    )
    cached_schemas[creating_proc] = sub_schema_map
    return sub_schema_map


def _resolve_all_cross_procedures(
    schema_map: dict[str, TempTableSchema],
    current_proc: str,
    conn,
    visited: set,
    dialect: str,
    tmp_proc_map: dict,
    cached_schemas: dict,
    depth: int = 0,
):
    """扫描 schema_map 中所有 tmp 表，触发跨过程追溯并合并子过程的 schema"""
    def _check_and_merge(src_tmp: str):
        if src_tmp in schema_map:
            return
        if src_tmp not in tmp_proc_map:
            return
        creating_proc, _ = tmp_proc_map[src_tmp]
        if creating_proc == current_proc:
            return
        sub_schema = _resolve_cross_procedure(
            src_tmp, current_proc, conn, visited, dialect,
            tmp_proc_map, cached_schemas
        )
        if sub_schema:
            for sub_tbl, sub_schema_obj in sub_schema.items():
                if sub_tbl not in schema_map and _is_temp_table(sub_tbl):
                    schema_map[sub_tbl] = sub_schema_obj
        # 递归：子过程中引用的 tmp 也需要穿透
        _resolve_all_cross_procedures(
            schema_map, current_proc, conn, visited, dialect,
            tmp_proc_map, cached_schemas, depth=depth+1
        )

    seen: set = set()
    changed = True
    while changed and depth < 5:
        changed = False
        for tbl in list(schema_map.keys()):
            if not _is_temp_table(tbl):
                continue
            for src_tmp in schema_map[tbl].source_table_names:
                if _is_temp_table(src_tmp) and src_tmp not in schema_map and src_tmp not in seen:
                    seen.add(src_tmp)
                    _check_and_merge(src_tmp)
                    changed = True
            # Fix 5: 同时检查 _referenced_tmp_names
            for src_tmp in schema_map[tbl].get_referenced_tmp_names():
                if _is_temp_table(src_tmp) and src_tmp not in schema_map and src_tmp not in seen:
                    seen.add(src_tmp)
                    _check_and_merge(src_tmp)
                    changed = True


class ProcedureLineageParserV2:
    """支持跨过程追溯的血缘解析器"""

    def __init__(self, db_type: str = "postgresql", dialect: str = "postgres"):
        self.db_type = db_type
        self.dialect = dialect
        self._tmp_proc_map: dict = {}
        self._cached_schemas: dict = {}

    def parse_full(self, proc_name: str, schema: str, conn) -> tuple[dict, dict]:
        if not self._tmp_proc_map:
            self._tmp_proc_map = conn.build_temp_table_procedure_map()
        self._cached_schemas.clear()
        visited: set = set()
        raw = conn.get_procedure_source(proc_name, schema)
        body = extract_body(raw)
        stmts = split_statements(body, self.dialect)
        schema_map = build_schema_map(stmts, self.dialect, conn)
        _resolve_all_cross_procedures(
            schema_map, proc_name,
            conn, visited, self.dialect,
            self._tmp_proc_map, self._cached_schemas,
            depth=0,
        )
        result_tables = [t for t in schema_map if not _is_temp_table(t)]
        result_tables_lineage = []
        for tbl in result_tables:
            cols = schema_map[tbl].get_all_columns()
            lineage = trace_lineage(schema_map, tbl)
            result_tables_lineage.append({
                "table": tbl,
                "columns": len(cols),
                "lineage": lineage,
            })
        result = {
            "procedure": proc_name,
            "db_type": self.db_type,
            "result_tables": result_tables_lineage,
        }
        return result, schema_map

    def validate_lineage(
        self, schema_map: dict, result_tables: list[str], registry: set[str]
    ) -> list[dict]:
        return ProcedureLineageParser().validate_lineage(schema_map, result_tables, registry)
