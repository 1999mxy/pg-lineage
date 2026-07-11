pg-lineage: PostgreSQL 存储过程字段血缘分析 Skill
=====================================================

一、这是什么
-----------
pg-lineage 是一个 Codex skill，用于对 PostgreSQL 存储过程进行字段级血缘分析。
给定一个存储过程名称，它会解析该存储过程的源码，追溯结果表中每个字段的血缘链路，
找出该字段最终来自哪张最上层来源表的哪个字段，以及经过了什么样的变换。


二、核心功能
-----------
1. 字段级血缘追溯
   对结果表的每个字段，追溯到 src_ 开头的最上层来源表及字段。

2. 变换类型识别
   自动区分以下变换类型：
   - 直接引用：直接取自某张来源表字段
   - CASE WHEN 条件映射：条件分支映射
   - 算术计算：+ - * /
   - 算术函数计算：ROUND / ABS / TRUNC 等
   - 字符串拼接：CONCAT / ||
   - 空值处理：COALESCE / NVL / IFNULL
   - 聚合/窗口函数：SUM / COUNT / AVG / MAX / MIN
   - 类型转换：CAST / ::

3. expression 透传
   跨多层临时表回溯完整表达式，自动替换 tmp.col 为真实的 src_table.col。

4. 跨存储过程追溯
   当存储过程 A 引用了存储过程 B 创建的临时表时，
   自动定位 B 的源码并递归解析血缘链。

5. 动态 SQL 支持（方案 B）
   识别 EXECUTE 语句中的 SQL 文本和 FROM 表名，
   列名不全时从 information_schema 回查源表列清单。

6. 来源表注册校验
   所有追溯到的最上层表必须在 lineage_source_table_registry 表中，
   否则报警，防止临时表未被完全穿透。


三、使用方式
-----------
在 Codex 对话中直接说：

  帮我用 pg-lineage 分析 tristan 数据库里的 p_gen_sale_report 存储过程

Codex 会自动读取此 skill 并调用 run.py 执行。

命令行直接调用：

  cd ~/codex-skills/pg-lineage
  python3 run.py -H localhost -P 5432 -d tristan -u tristan     --proc p_gen_sale_report --schema public --format summary

参数说明：
  -H / --host      数据库地址，默认 localhost
  -P / --port      端口，默认 5432
  -d / --db        数据库名（必填）
  -u / --user      用户名（必填）
  -p / --password  密码，默认空
  --proc           存储过程名（必填）
  -s / --schema    Schema，默认 public
  -f / --format    输出格式：summary / mapping / json / csv / sql
  --dialect        SQL 方言，默认 postgres
  --cross-proc     启用跨存储过程追溯
  --validate-registry  启用来源表注册校验


四、输出格式
-----------
1. summary（默认）
   按最上层来源表分组，列出结果表的每个字段归属哪张来源表。
   示例：
     存储过程: p_gen_sale_report
     结果表: result_sale_report (21 列)
       └── src_customer: customer_name, customer_segment, vip_level
       └── src_orders: customer_id, net_order_amount, order_date, ...
       └── src_orders_detail: quantity, subtotal, unit_price, ...

2. mapping
   结构化表格，每行展示：
   结果字段 | 来源(表.字段) | 变换类型 | 表达式 | 完整链路(表.字段)
   包含字段级变换路径，CASE WHEN / 算术计算等派生表达式清晰可见。

3. json / csv / sql
   程序化消费格式，json 为完整血缘数据，csv 可导入 Excel，
   sql 生成写入 lineage_column 表的 INSERT 语句。


五、数据准备
-----------
血缘分析前需要在数据库中创建来源表注册表：

  CREATE TABLE lineage_source_table_registry (
      table_name VARCHAR(100) PRIMARY KEY
  );

  INSERT INTO lineage_source_table_registry VALUES
      ('src_customer'), ('src_customer_address'),
      ('src_orders'), ('src_orders_detail'),
      ('src_product'), ('src_product_inventory');

这样 --validate-registry 才能正常工作。


六、约束
-------
- 临时表命名必须以 tmp 开头，如 tmp1_cust_addr、tmp_combined_orders
- 源表命名以 src_ 开头，结果表命名不含 tmp / src_ 前缀
- 存储过程内不写 SELECT * INTO 变量（into 变量不做血缘分析）
- 动态 SQL 中，变量拼接的表名无法静态还原，字段级血缘降级为近似值


七、Python API
-------------
from src.connectors.postgres_conn import PostgresConnector
from src.parser import ProcedureLineageParser, ProcedureLineageParserV2
from src.output.formatter import summary, to_mapping

conn = PostgresConnector(host='localhost', port=5432, db='tristan', user='tristan')
conn.connect()

# 简单模式
parser = ProcedureLineageParser(dialect='postgres')
result, schema_map = parser.parse_full('p_gen_sale_report', 'public', conn)
print(summary(result, schema_map))

# 跨过程模式
parser2 = ProcedureLineageParserV2(dialect='postgres')
result2, schema_map2 = parser2.parse_full('p_cross_proc_demo', 'public', conn)
print(to_mapping(result2, schema_map2))

conn.close()


八、扩展方向
-----------
- 接入 Oracle / TDSQL connector，实现多数据库支持
- 增强动态 SQL 的列级别精确度（当前依赖 metadata 回查）
- 血缘结果持久化到数据库表，支持历史版本对比


九、目录结构
-----------
pg-lineage/
  SKILL.md              - Codex skill 定义
  README.md             - Markdown 版文档
  README.txt            - 本文件
  run.py                - 命令行入口
  src/
    parser.py           - 核心解析器（~1100行）
    connectors/
      postgres_conn.py  - PostgreSQL 连接器
      oracle_conn.py    - Oracle 连接器（待实现）
      tdsql_conn.py     - TDSQL 连接器（待实现）
    output/
      formatter.py      - 输出格式化（summary/json/csv/sql）
      mapping_fmt.py    - 字段血缘 mapping 文档
  tests/
