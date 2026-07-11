"""血缘输出格式化器"""
import json
import re
from collections import defaultdict


def to_json(result: dict, indent: int = 2) -> str:
    return json.dumps(result, indent=indent, ensure_ascii=False)


def to_csv(result: dict) -> str:
    lines = []
    lines.append("result_table,result_column,src_schema,src_table,src_column,lineage_type,expression,chain")
    for tbl_info in result.get("result_tables", []):
        for row in tbl_info.get("lineage", []):
            fields = [
                tbl_info["table"],
                row["result_column"],
                row.get("src_schema", ""),
                row["src_table"],
                row["src_column"],
                row["lineage_type"],
                (row.get("expression") or "").replace('"', '""'),
                row.get("chain", ""),
            ]
            lines.append(",".join(f'"{f}"' for f in fields))
    return "\n".join(lines)


def _sql_escape(s):
    if s is None:
        return "NULL"
    return s.replace("'", "''")


def to_sql_insert(result: dict) -> str:
    lines = []
    for tbl_info in result.get("result_tables", []):
        for row in tbl_info.get("lineage", []):
            chain = row.get("chain", "") or ""
            line = (
                f"INSERT INTO lineage_column "
                f"(proc_id, result_table, result_column, "
                f"src_schema, src_table, src_column, "
                f"lineage_type, chain) "
                f"SELECT proc_id, '{_sql_escape(tbl_info['table'])}', "
                f"'{_sql_escape(row['result_column'])}', "
                f"'{_sql_escape(row.get('src_schema', 'public'))}', "
                f"'{_sql_escape(row['src_table'])}', "
                f"'{_sql_escape(row['src_column'])}', "
                f"'{_sql_escape(row['lineage_type'])}', "
                f"'{_sql_escape(chain)}' "
                f"FROM lineage_proc WHERE proc_name = '{_sql_escape(result['procedure'])}';"
            )
            lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------
# 工具函数：按 root_table 分组追溯
# ---------------------------------------------------------------

def _trace_column_for_group(schema_map: dict, src_table: str, src_col: str, depth: int = 0) -> tuple[str, str, list[str]]:
    """
    用于分组的追溯：返回 (root_table, root_col, chain)。
    递归穿透 tmp 表链直至最上层非 tmp 表。

    P0-3 修复（完整版）:
    - 回退逻辑增强：扫描当前 tmp 表的所有列，找任意指向其他 tmp 表的列，
      递归追溯后继续回退。处理 SELECT * 继承导致某列不在当前 tmp schema 的情况。
    """
    if depth > 30 or not src_table:
        return "", "", []
    if not re.match(r"^tmp", src_table):
        return src_table, src_col, [src_table]
    if src_table not in schema_map:
        return src_table, src_col, [src_table]

    src = schema_map[src_table].get_column(src_col)
    if src and src.src_table:
        root_t, root_c, tail = _trace_column_for_group(
            schema_map, src.src_table, src.src_column, depth + 1
        )
        return root_t, root_c, [src_table] + tail

    # 回退：列不在当前 tmp 表的 schema 中
    # 1. 从 source_table_names + referenced_tmp_names 回退
    # 2. 扫描所有列找指向其他 tmp 表的列，递归追溯后继续
    all_candidates = (
        schema_map[src_table].source_table_names |
        schema_map[src_table].get_referenced_tmp_names()
    )
    for cand in sorted(all_candidates):
        if cand == src_table or not re.match(r"^tmp", cand):
            continue
        if cand in schema_map:
            cand_src = schema_map[cand].get_column(src_col)
            if cand_src and cand_src.src_table:
                root_t, root_c, tail = _trace_column_for_group(
                    schema_map, cand_src.src_table, cand_src.src_column, depth + 1
                )
                return root_t, root_c, [cand] + tail

    # 扫描所有列，找任意指向其他 tmp 表的列，递归追溯后继续回退
    for other_col, other_src in schema_map[src_table].get_all_columns().items():
        if not other_col or not other_src:
            continue
        if not re.match(r"^tmp", other_src.src_table):
            continue
        # 递归追溯 other_col 到 root，再继续回退
        sub_root_t, sub_root_c, sub_tail = _trace_column_for_group(
            schema_map, other_src.src_table, other_src.src_column, depth + 1
        )
        if sub_root_t and not re.match(r"^tmp", sub_root_t):
            return sub_root_t, sub_root_c, [src_table] + sub_tail

    return src_table, src_col, [src_table]


# ---------------------------------------------------------------
# summary: 按最上层来源表分组
#
# P0-3 修复逻辑：
#   - direct 字段：只通过 src_table/src_column 追溯，归因一次
#   - derived 字段：每个 src_columns 条目独立追溯，允许出现在多个 root 分组
#   - 回退追溯：当 tmp 表中某列不在 schema_map 中时，查找其他 tmp 表继续追溯
#     （处理 SELECT * 继承导致的列穿透，如 discount 列穿透 tmp4_order_cust）
# ---------------------------------------------------------------

def summary(result: dict, schema_map: dict = None) -> str:
    lines_out = [f"存储过程: {result['procedure']}", ""]
    sm = schema_map or {}

    for tbl_info in result.get("result_tables", []):
        lines_out.append(f"结果表: {tbl_info['table']} ({tbl_info['columns']} 列)")
        groups: dict = defaultdict(set)
        # direct 字段去重
        direct_attributed: set = set()

        def _get_root(t: str, c: str) -> str:
            if t and re.match(r"^tmp", t) and sm:
                root, _, _ = _trace_column_for_group(sm, t, c)
                return root
            return t

        for row in tbl_info.get("lineage", []):
            result_col = row["result_column"]
            lt = row.get("lineage_type", "")
            src_t = row.get("src_table") or ""
            src_c = row.get("src_column") or ""
            src_cols = row.get("src_columns", [])

            if lt == "direct":
                if result_col in direct_attributed:
                    continue
                root = _get_root(src_t, src_c)
                if root:
                    groups[root].add(result_col)
                    direct_attributed.add(result_col)
            else:
                # derived: 每个 src_columns 条目独立追溯
                cols_to_trace = src_cols if src_cols else [(src_t, src_c)]
                seen_roots = set()
                for (s_t, s_c) in cols_to_trace:
                    if not s_t:
                        continue
                    root = _get_root(s_t, s_c)
                    if root and root not in seen_roots:
                        seen_roots.add(root)
                        groups[root].add(result_col)

        for src_tbl, cols in sorted(groups.items()):
            cols_str = ", ".join(sorted(cols))
            lines_out.append(f"  └── {src_tbl}: {cols_str}")

        lines_out.append("")

    return "\n".join(lines_out)


try:
    from .mapping_fmt import to_mapping
except ImportError:
    def to_mapping(result: dict, schema_map: dict = None) -> str:
        return summary(result, schema_map)
