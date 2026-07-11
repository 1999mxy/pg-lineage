"""
Mapping 格式输出（增强版）

Fix 3: 按 result_column 聚合多源字段，一行展示所有来源（逗号分隔）
Fix 1+2: expression 列使用 _substitute_expr 简化 tmp 前缀
P0-1: CJK 字符按显示宽度（2格）计算列宽
"""

import re
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from src.parser import (
    _is_temp_table, _trace_column, _substitute_expr,
    _is_simple_column_ref, TempTableSchema
)


# ---------------------------------------------------------------
# CJK 字符显示宽度计算
# ---------------------------------------------------------------

def _display_width(s: str) -> int:
    """
    计算字符串在终端的显示宽度。
    CJK 字符（中文、日文、韩文）占 2 格，ASCII 占 1 格。
    """
    w = 0
    for ch in s:
        if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f' \
                or '\uff00' <= ch <= '\uffef' or '\uac00' <= ch <= '\ud7a3':
            w += 2
        elif ord(ch) > 127:
            w += 2
        else:
            w += 1
    return w


# ---------------------------------------------------------------
# 表达式 TMP 前缀替换（复刻自 parser.py，确保独立可用）
# ---------------------------------------------------------------

def _substitute_expr_mapping(expr: str, schema_map: dict) -> str:
    """对表达式做 tmp.col -> src_.col 替换，简化阅读"""
    if not expr:
        return ""
    return _substitute_expr(expr, schema_map)


# ---------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------

def _type_label(lineage_type: str, expression: str) -> str:
    """根据血缘类型和表达式内容返回精确的中文类型标签"""
    if lineage_type == "direct":
        return "直接引用"
    low = (expression or "").upper()
    if "CASE" in low and "WHEN" in low:
        return "CASE WHEN 条件映射"
    if any(op in low for op in ["+", "-", "*", "/"]) and any(
        fn in low for fn in ["ROUND", "ABS", "TRUNC", "POWER", "SQRT", "LOG"]
    ):
        return "算术函数计算"
    if any(op in low for op in ["+", "-", "*", "/"]):
        return "算术计算"
    if "CONCAT" in low or "||" in low:
        return "字符串拼接"
    if any(fn in low for fn in ["COALESCE", "NVL", "IFNULL", "NULLIF", "GREATEST", "LEAST"]):
        return "空值处理"
    if any(fn in low for fn in ["SUM", "COUNT", "AVG", "MAX", "MIN",
                                  "DENSE_RANK", "RANK", "ROW_NUMBER"]):
        return "聚合/窗口函数"
    if any(fn in low for fn in ["LAG", "LEAD", "OVER", "PARTITION"]):
        return "窗口函数"
    if any(fn in low for fn in ["CAST", "::"]):
        return "类型转换"
    if expression:
        return "函数变换"
    return "派生字段"


# ---------------------------------------------------------------
# Fix 3: 按 result_column 聚合，一行多源字段
# ---------------------------------------------------------------

def _build_mapping_rows(
    tbl_info: dict,
    schema_map: dict,
) -> list[list[str]]:
    """
    为单个结果表构建 mapping 表格行。
    Fix 3: 每个 result_column 只占一行，多个来源字段用换行符分隔显示。
    列: 结果字段 | 来源(表.字段) | 变换类型 | 表达式 | 完整链路
    """
    lineage = tbl_info.get("lineage", [])
    if not lineage:
        return []

    sm = schema_map or {}

    # 按 result_column 聚合
    by_col: dict = {}
    for row in lineage:
        col_name = row["result_column"]
        if col_name not in by_col:
            by_col[col_name] = []
        by_col[col_name].append(row)

    rows = []
    for col_name in sorted(by_col.keys()):
        records = by_col[col_name]

        # 收集去重后的源信息
        src_entries: list[tuple] = []
        seen = set()
        for r in records:
            src_t = r.get("src_table") or ""
            src_c = r.get("src_column") or ""
            lt = r.get("lineage_type", "")
            expr = r.get("expression") or ""
            chain_str = r.get("chain") or ""

            # 追溯到 root_table
            if src_t and _is_temp_table(src_t) and sm:
                root_t, _, chain = _trace_to_root(sm, src_t, src_c)
                chain_str = " -> ".join(chain) if chain else chain_str
            else:
                root_t = src_t

            key = (root_t, src_c, lt)
            if key not in seen:
                seen.add(key)
                src_entries.append((root_t, src_c, lt, expr, chain_str))

        # 主来源用于类型判断
        primary_lt = src_entries[0][2] if src_entries else "direct"
        primary_expr = src_entries[0][3] if src_entries else ""
        type_lbl = _type_label(primary_lt, primary_expr)

        # 合并所有来源字段
        src_descs: list[str] = []
        for root_t, src_c, lt, expr, chain_str in src_entries:
            if root_t and src_c:
                src_descs.append(f"{root_t}.{src_c}")
            elif root_t:
                src_descs.append(root_t)

        # 合并所有链路
        chain_descs = [cs for _, _, _, _, cs in src_entries if cs]
        chain_display = chain_descs[0] if chain_descs else ""

        # 表达式化简
        expr_display = _substitute_expr_mapping(primary_expr, sm) if primary_expr else ""
        if len(expr_display) > 80:
            expr_display = expr_display[:80] + "..."

        # 来源字段（多个来源用换行分隔）
        src_display = "\n".join(src_descs) if src_descs else ""

        rows.append([
            col_name,
            src_display,
            type_lbl,
            expr_display,
            chain_display,
        ])

    return rows


def _trace_to_root(
    schema_map: dict,
    src_table: str,
    src_col: str,
    depth: int = 0,
) -> tuple[str, str, list[str]]:
    """通过 schema_map 追溯到最上层非 tmp 表"""
    if depth > 30 or not src_table:
        return "", "", []
    if not _is_temp_table(src_table):
        return src_table, src_col, [src_table]
    if src_table not in schema_map:
        return src_table, src_col, [src_table]
    src = schema_map[src_table].get_column(src_col)
    if not src or not src.src_table:
        return src_table, src_col, [src_table]
    root_t, root_c, tail = _trace_to_root(
        schema_map, src.src_table, src.src_column, depth + 1
    )
    return root_t, root_c, [src_table] + tail


# ---------------------------------------------------------------
# P0-1: 动态列宽——按 CJK 显示宽度计算
# ---------------------------------------------------------------

def _calc_col_widths(header: list[str], rows: list[list[str]], min_widths: list[int]) -> list[int]:
    """
    根据内容计算每列宽度（考虑 CJK 字符占 2 格）。
    min_widths: 各列的最小宽度。
    """
    num_cols = len(header)
    widths = list(min_widths) if min_widths else [0] * num_cols

    # 遍历所有行（含表头），取最大显示宽度
    for row in [header] + rows:
        for i, cell in enumerate(row):
            if i < num_cols:
                w = _display_width(str(cell) if cell else "")
                widths[i] = max(widths[i], w)

    return widths


def _render_table(rows: list[list[str]], col_widths: list[int]) -> list[str]:
    """用动态列宽渲染 ASCII 表格"""
    sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    result = [sep]
    for ri, row in enumerate(rows):
        line = "|"
        for i, (cell, w) in enumerate(zip(row, col_widths)):
            cell_s = str(cell) if cell else ""
            # 右填充到显示宽度 w
            padded = _pad_display(cell_s, w)
            line += " " + padded + " |"
        result.append(line)
        if ri == 0:
            result.append(sep.replace("-", "="))
        else:
            result.append(sep)
    return result


def _pad_display(s: str, width: int) -> str:
    """将字符串右填充到显示宽度"""
    current = _display_width(s)
    return s + " " * (width - current)


# ---------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------

def to_mapping(result: dict, schema_map: dict = None) -> str:
    """
    生成字段血缘 mapping 文档（结构化表格版）。
    P0-1: 使用 CJK 感知列宽算法，表格对齐美观。
    列: 结果字段 | 来源(表.字段) | 变换类型 | 表达式 | 完整链路
    """
    sm = schema_map or {}
    out_lines = []

    out_lines.append("=" * 80)
    out_lines.append("  字段血缘 Mapping 文档")
    out_lines.append("=" * 80)
    out_lines.append(f"  存储过程 : {result['procedure']}")
    out_lines.append(f"  数据库   : {result['db_type']}")
    out_lines.append("")

    # 最小列宽（表头宽度 + 留白）
    min_widths = [16, 24, 14, 40, 22]
    header = ["结果字段", "来源(表.字段)", "变换类型", "表达式", "完整链路"]

    for tbl_info in result.get("result_tables", []):
        tbl_name = tbl_info["table"]
        col_count = tbl_info["columns"]

        out_lines.append(f"  结果表 : {tbl_name}  ({col_count} 列)")
        out_lines.append("-" * 80)

        rows = _build_mapping_rows(tbl_info, sm)
        if not rows:
            out_lines.append("  (无血缘数据)")
            out_lines.append("")
            continue

        # P0-1: 动态计算列宽
        col_widths = _calc_col_widths(header, rows, min_widths)

        # 渲染表格
        table_lines = _render_table([header] + rows, col_widths)
        for tline in table_lines:
            out_lines.append("  " + tline)

        out_lines.append("")

        # 变换类型说明
        type_counts: dict = {}
        for row in rows:
            lt = row[2]
            type_counts[lt] = type_counts.get(lt, 0) + 1
        legend = " | ".join(f"{k}({v})" for k, v in sorted(type_counts.items()))
        out_lines.append(f"  变换类型统计: {legend}")
        out_lines.append("")
        out_lines.append("=" * 80)

    return "\n".join(out_lines)
