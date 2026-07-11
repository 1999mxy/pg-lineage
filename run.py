#!/usr/bin/env python3
"""
pg-lineage 命令行入口

用法示例：
    cd ~/codex-skills/pg-lineage
    python run.py --host localhost --port 5432 --db tristan --user tristan \
        --proc p_gen_sale_report --schema public --format summary

    python run.py -H localhost -P 5432 -d tristan -u tristan \
        --proc p_gen_sale_report --schema public --format mapping --validate-registry

输出格式：
  summary  - 文本摘要，按来源表分组显示（默认）
  json     - 完整血缘 JSON
  csv      - CSV 格式血缘明细
  sql      - 可直接写入 lineage_column 表的 INSERT 语句
  mapping  - 字段映射文档
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse

from src.connectors.postgres_conn import PostgresConnector
from src.connectors.oracle_conn   import OracleConnector
from src.connectors.tdsql_conn    import TDSqlConnector
from src.parser import ProcedureLineageParser, ProcedureLineageParserV2
from src.output.formatter import to_json, to_csv, summary, to_sql_insert, to_mapping


def parse_args():
    parser = argparse.ArgumentParser(
        description="PostgreSQL 存储过程字段血缘分析工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", "-H", default="localhost")
    parser.add_argument("--port", "-P", type=int, default=5432)
    parser.add_argument("--db", "-d", required=True)
    parser.add_argument("--user", "-u", required=True)
    parser.add_argument("--password", "-p", default="")
    parser.add_argument("--proc", required=True)
    parser.add_argument("--schema", "-s", default="public")
    parser.add_argument("--format", "-f",
                        choices=["summary", "json", "csv", "sql", "mapping"],
                        default="summary")
    parser.add_argument("--indent", "-i", type=int, default=2)
    parser.add_argument("--dialect", default="postgres",
                        choices=["postgres", "oracle", "mysql", "tsql"],
                        help="SQL dialect for parser (default postgres)")
    parser.add_argument("--cross-proc", action="store_true",
                        help="跨过程追溯：穿透子过程临时表至最上层源表")
    parser.add_argument("--validate-registry", action="store_true",
                        help="启用来源表注册校验")
    parser.add_argument("--registry-table", default="lineage_source_table_registry",
                        help="来源表注册表表名，默认 lineage_source_table_registry")
    return parser.parse_args()


def main():
    args = parse_args()

    # 统一写到文件，绕过 PTY 缓冲导致的输出丢失
    out_file = open("/tmp/lineage_stdout.txt", "w")
    err_file = open("/tmp/lineage_stderr.txt", "w")

    def info(msg):
        err_file.write("[pg-lineage] " + msg + "\n")

    conn_desc = "{}:{}/{}".format(
        args.host, args.port, args.db
    )
    info("连接 {}，解析存储过程 {}.{} ...".format(conn_desc, args.schema, args.proc))

    try:
        dialect_map = {
            "postgres": PostgresConnector,
            "oracle":   OracleConnector,
            "mysql":    TDSqlConnector,
            "tsql":     TDSqlConnector,
        }
        ConnClass = dialect_map.get(args.dialect, PostgresConnector)
        conn = ConnClass(
            host=args.host,
            port=args.port,
            db=args.db,
            user=args.user,
            password=args.password
        )
        conn.connect()

        ParserClass = ProcedureLineageParserV2 if args.cross_proc else ProcedureLineageParser
        parser = ParserClass(db_type="postgresql", dialect=args.dialect)
        result, schema_map = parser.parse_full(args.proc, args.schema, conn)

        # 校验
        validation_warnings = []
        tbl_exists = False
        if args.validate_registry:
            registry, tbl_exists = conn.get_source_table_registry(args.registry_table)
            result_tables = [rt["table"] for rt in result.get("result_tables", [])]
            if not tbl_exists:
                msg = (
                    "[pg-lineage] 注册表 [" + args.registry_table + "] 不存在，跳过校验。\n"
                    "[pg-lineage] 如需启用校验，请先创建注册表：\n"
                    "  CREATE TABLE " + args.registry_table + " (table_name VARCHAR(100) PRIMARY KEY);\n"
                    "  INSERT INTO " + args.registry_table + " VALUES ('your_source_table');\n"
                )
                err_file.write(msg)
            else:
                validation_warnings = parser.validate_lineage(schema_map, result_tables, registry)

        # 输出
        if args.format == "json":
            out_file.write(to_json(result, indent=args.indent))
        elif args.format == "csv":
            out_file.write(to_csv(result))
        elif args.format == "sql":
            out_file.write(to_sql_insert(result))
        elif args.format == "mapping":
            out_file.write(to_mapping(result, schema_map))
        else:
            out_file.write(summary(result, schema_map))

        out_file.close()

        if validation_warnings:
            sep = "=" * 70
            err_file.write(sep + "\n")
            err_file.write("[WARNING] 血缘校验：以下字段的来源表未在 registry 中注册\n")
            err_file.write(sep + "\n")
            err_file.write("{:<30}  {:<25}  {:<25}  {}\n".format(
                "result_table","result_column","root_table","reason"))
            err_file.write("-" * 120 + "\n")
            for w in validation_warnings:
                err_file.write("{:<30}  {:<25}  {:<25}  {}\n".format(
                    w["result_table"], w["result_column"], w["root_table"], w["reason"]))
            err_file.write("-" * 120 + "\n")
            err_file.write("共 {} 条警告\n".format(len(validation_warnings)))
            err_file.write(sep + "\n")
        elif args.validate_registry and tbl_exists:
            err_file.write("[pg-lineage] All source tables are registered. Validation passed.\n")

        err_file.close()
        conn.close()

        # 读取并打印，绕过 PTY 缓冲
        sys.stdout.write(open("/tmp/lineage_stdout.txt").read())
        sys.stderr.write(open("/tmp/lineage_stderr.txt").read())

    except Exception as e:
        err_file.write("[pg-lineage] 错误: {}\n".format(e))
        err_file.close()
        out_file.close()
        sys.stderr.write(open("/tmp/lineage_stderr.txt").read())
        sys.exit(1)


if __name__ == "__main__":
    main()
