# pg-lineage: 存储过程字段血缘分析

## 功能

对 PostgreSQL / Oracle / TDSQL 存储过程（PROCEDURE / FUNCTION）进行**字段级血缘分析**：
给定一个存储过程名称，追溯结果表中每个字段最终来自哪张最上层来源表、哪个源字段，
以及经历了什么类型的变换（CASE WHEN / 算术计算 / 窗口函数 / 字符串拼接 等）。

当存储过程 A 引用了存储过程 B 创建的临时表时，自动递归解析 B 的源码，
将血缘链完整合并进来。

支持输出 summary（按源表分组）、mapping（结构化明细）、json、csv、sql 五种格式。
可通过 `--validate-registry` 校验所有追溯到的源表是否已在注册表中。
   表名可通过 `--registry-table` 参数自定义，不同数据库可使用不同的注册表。

**适用场景**：数据仓库贴源层建模、ETL 血缘治理、存储过程重构影响分析。

## 约束

- 临时表命名必须以 `tmp` 开头（如 `tmp1_cust_addr`、`tmp_combined_orders`）
- 源表命名建议以 `src_` 开头，结果表命名不含 `tmp`/`src_` 前缀。
   所有可作为血缘终点的来源表须预先写入注册表（可通过 --registry-table 自定义，默认 lineage_source_table_registry）。
- 存储过程内不写 `SELECT *`
- 结果表命名不含 `tmp` 前缀

## 目录结构

```
pg-lineage/
├── SKILL.md
├── run.py                      # 命令行入口
├── src/
│   ├── parser.py               # 核心解析器
│   ├── connectors/
│   │   ├── postgres_conn.py   # PostgreSQL 连接器
│   │   ├── oracle_conn.py     # Oracle 连接器
│   │   └── tdsql_conn.py      # TDSQL 连接器
│   └── output/
│       ├── formatter.py        # 输出格式化（summary / json / csv / sql）
│       └── mapping_fmt.py      # 字段血缘 mapping 文档
└── tests/
    └── test_*.py
```

## 使用方式

### 命令行

```bash
cd ~/codex-skills/pg-lineage

# 文本摘要（按来源表分组）
python3 run.py \
    -H localhost -P 5432 -d tristan -u tristan \
    --proc p_gen_sale_report --schema public \
    --format summary --validate-registry

# 字段血缘 mapping 文档
python3 run.py \
    -H localhost -P 5432 -d tristan -u tristan \
    --proc p_gen_sale_report --schema public \
    --format mapping

# 跨过程追溯（存储过程引用了其他过程的临时表时使用）
python3 run.py \
    -H localhost -P 5432 -d tristan -u tristan \
    --proc p_cross_proc_demo --schema public \
    --format summary --validate-registry --cross-proc
```

### 参数说明

| 参数 | 必填 | 说明 |
|------|------|------|
| `--host` / `-H` | 否 | 数据库地址，默认 localhost |
| `--port` / `-P` | 否 | 端口，默认 5432 |
| `--db` / `-d` | 是 | 数据库名 |
| `--user` / `-u` | 是 | 用户名 |
| `--password` / `-p` | 否 | 密码，默认空 |
| `--proc` | 是 | 存储过程名称 |
| `--schema` / `-s` | 否 | Schema，默认 public |
| `--format` / `-f` | 否 | 输出格式，默认 summary |
| `--cross-proc` | 否 | 启用跨过程追溯 |
| `--validate-registry` | 否 | 启用来源表注册校验 |
| `--registry-table` | 否 | 注册表表名（默认 lineage_source_table_registry，可按库自定义） |
| `--dialect` | 否 | SQL 方言：postgres / oracle / mysql / tsql，默认 postgres |

### 输出格式

| 格式 | 说明 |
|------|------|
| `summary` | 文本摘要，按最上层来源表分组 |
| `mapping` | 结构化字段血缘 mapping 文档（表格格式） |
| `json` | 完整血缘 JSON |
| `csv` | CSV 格式血缘明细 |
| `sql` | 可写入 lineage_column 表的 INSERT 语句 |

### Codex 直接调用

在 Codex 对话中直接说：
```
帮我用 pg-lineage 分析 tristan 数据库里的 p_gen_sale_report 存储过程
```

支持多数据库，按 `--dialect` 参数选择连接器：

| dialect | 连接器 | 驱动 |
|---------|--------|------|
| `postgres` | PostgresConnector | psycopg2 |
| `oracle` | OracleConnector | oracledb |
| `mysql` | TDSqlConnector | pymysql |
| `tsql` | TDSqlConnector | pymysql |
Codex 会自动读取此 skill 并调用 `run.py` 执行。

### Python API

```python
import sys
sys.path.insert(0, "~/codex-skills/pg-lineage")

from src.connectors.postgres_conn import PostgresConnector
from src.connectors.oracle_conn   import OracleConnector
from src.connectors.tdsql_conn    import TDSqlConnector
from src.parser import ProcedureLineageParser, ProcedureLineageParserV2
from src.output.formatter import to_json, summary, to_mapping

# PostgreSQL
conn = PostgresConnector(host="localhost", port=5432, db="tristan", user="tristan")

# Oracle
# conn = OracleConnector(host="localhost", port=1521, db="XE", user="SCOTT", password="tiger")

# TDSQL / MySQL
# conn = TDSqlConnector(host="localhost", port=3306, db="test", user="root", password="")

conn.connect()

# 简单模式（不跨过程）
parser = ProcedureLineageParser(db_type="postgresql", dialect="postgres")
result, schema_map = parser.parse_full("p_gen_sale_report", "public", conn)

# 跨过程模式
parser2 = ProcedureLineageParserV2(db_type="postgresql", dialect="postgres")
result2, schema_map2 = parser2.parse_full("p_cross_proc_demo", "public", conn)

print(summary(result, schema_map))
print(to_mapping(result, schema_map))
```

## 验证命令

```bash
cd ~/codex-skills/pg-lineage
python3 run.py -H localhost -P 5432 -d tristan -u tristan \
    --proc p_gen_sale_report --schema public --format summary --validate-registry --registry-table lineage_source_table_registry

python3 run.py -H localhost -P 5432 -d tristan -u tristan \
    --proc p_sale_analysis --schema public --format summary --validate-registry --registry-table lineage_source_table_registry

python3 run.py -H localhost -P 5432 -d tristan -u tristan \
    --proc p_inventory_analysis --schema public --format summary --validate-registry --registry-table lineage_source_table_registry

python3 run.py -H localhost -P 5432 -d tristan -u tristan \
    --proc p_cross_proc_demo --schema public --format summary --validate-registry --cross-proc --registry-table lineage_source_table_registry
```

## 核心解析逻辑

### 正向解析（build_schema_map）

1. `split_statements()`：按分号切分存储过程体，识别 `CREATE [TEMP] TABLE ... AS SELECT`、`CREATE [TEMP] TABLE ... (cols)`、`INSERT INTO ... SELECT` 三类语句
2. `_build_select_expanded()`：用 sqlglot AST 精准提取表别名，展开列引用中的别名（如 `o.order_id` → `src_orders.order_id`）
3. `parse_select()`：解析 SELECT SQL，提取每个结果列的来源表、来源列、是否派生
4. 处理 `SELECT *`：从上游临时表继承 ColumnSource

### 逆向追溯（trace_lineage）

1. `_trace_column()`：递归追溯单个列至最上层非 tmp 表，返回 (root_table, root_col, chain)
2. `_trace_single()`：对结果表每个列执行追溯，区分 direct（直接引用）和 derived（派生表达式）
3. `_fill_expr_from_chain()`：当 expression 为空时，递归向上回溯临时表链提取上游 expression

### 跨过程追溯（ProcedureLineageParserV2）

1. 通过 `pg_proc` + `pg_depend` 等元数据构建「临时表 → 创建它的存储过程」映射
2. 扫描当前存储过程所有临时表的 source_table_names
3. 对其中引用的其他过程的临时表，递归解析该子过程的源码，合并其 schema_map
4. visited 通过引用传递，防止循环追溯

### 来源表注册校验

数据库中需有 `lineage_source_table_registry` 表，存放合法的来源表名。血缘分析时：
- 对每个结果列追溯到最上层非 tmp 表
- 检查该表是否在 registry 中
- 不在 registry 中则报 WARNING，防止临时表未穿透就当成来源表

```sql
CREATE TABLE lineage_source_table_registry (
    table_name VARCHAR(100) PRIMARY KEY
);
INSERT INTO lineage_source_table_registry VALUES
    ('src_customer'), ('src_customer_address'),
    ('src_orders'), ('src_orders_detail'),
    ('src_product'), ('src_product_inventory');
```

## 输出示例

### summary（按来源表分组）

```
存储过程: p_gen_sale_report
结果表: result_sale_report (21 列)
  └── src_customer: customer_name, customer_segment, vip_level
  └── src_customer_address: address_tier, city, province
  └── src_orders: customer_id, net_order_amount, net_revenue, order_amount, order_date, order_id, profit_contribution
  └── src_orders_detail: quantity, subtotal, total_line_profit, unit_price
  └── src_product: brand, category, product_name, total_line_profit, unit_profit
```

### mapping（结构化表格）

```
================================================================================
  字段血缘 Mapping 文档
================================================================================
  存储过程 : p_gen_sale_report
  数据库   : postgresql

  结果表 : result_sale_report  (21 列)
--------------------------------------------------------------------------------
  +------------+---------------------+------------+------------------+-----------------------------------------------------...
  | 结果字段       | 来源表                  | 来源字段         | 变换类型          | 表达式                                               | ...
  +============+=====================+============+==================+=====================================================...
  | order_id   | src_orders          | order_id   | 直接引用          | tmp2_order_detail.order_id                          | ...
  | net_revenue| src_orders          | order_amount| CASE WHEN 条件映射 | CASE WHEN t4.vip_level = 'diamond' THEN ...           | ...
  ...
