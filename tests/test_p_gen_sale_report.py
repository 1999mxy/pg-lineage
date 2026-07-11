"""
测试用例：用 p_gen_sale_report 验证血缘分析器

测试策略：
1. 语句切分测试 - 验证能否正确拆分 6 条临时表语句
2. SELECT 列提取测试 - 验证 extract_select_columns
3. Schema Map 构建测试 - 验证临时表链路是否正确
4. 血缘追溯测试 - 验证结果表字段是否追溯到最上层来源表
5. 完整解析测试 - 端到端验证
6. 结果表过滤测试
7. 派生字段血缘（算术、CASE WHEN、函数）
8. 派生属性跨临时表透传

运行：
    cd pg-lineage
    python -m tests.test_p_gen_sale_report -v
"""

import re
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.parser import (
    extract_body,
    split_statements,
    extract_select_columns,
    parse_select,
    build_schema_map,
    trace_lineage,
    ProcedureLineageParser,
    ColumnSource,
    TempTableSchema,
)


# ---------------------------------------------------------------
# Fixture: p_gen_sale_report 源码（与数据库同步）
# ---------------------------------------------------------------

P_GEN_SALE_REPORT_SOURCE = """
CREATE OR REPLACE PROCEDURE public.p_gen_sale_report()
LANGUAGE plpgsql
AS $procedure$
BEGIN

    -- 临时表1：客户 + 地址（customer_id）
    DROP TABLE IF EXISTS tmp1_cust_addr;
    CREATE TEMP TABLE tmp1_cust_addr AS
    SELECT c.customer_id, c.customer_name, c.gender, c.age, c.customer_level,
           a.province, a.city, a.address
    FROM src_customer c
    LEFT JOIN src_customer_address a ON c.customer_id = a.customer_id;

    -- 临时表2：订单 + 订单明细（order_id）
    DROP TABLE IF EXISTS tmp2_order_detail;
    CREATE TEMP TABLE tmp2_order_detail AS
    SELECT o.order_id, o.customer_id, o.product_id, o.order_date, o.order_amount,
           o.discount, o.payment_method, o.order_status,
           d.unit_price, d.quantity, d.tax_amt
    FROM src_orders o
    LEFT JOIN src_orders_detail d ON o.order_id = d.order_id;

    -- 临时表3：tmp2 + 商品（product_id）
    DROP TABLE IF EXISTS tmp3_order_product;
    CREATE TEMP TABLE tmp3_order_product AS
    SELECT t.order_id, t.customer_id, t.product_id, t.order_date, t.order_amount,
           t.discount, t.payment_method, t.order_status, t.unit_price, t.quantity, t.tax_amt,
           p.product_name, p.category, p.brand, p.sell_price, p.stock_quantity
    FROM tmp2_order_detail t
    LEFT JOIN src_product p ON t.product_id = p.product_id;

    -- 临时表4：tmp3 + tmp1 客户地址（customer_id）
    DROP TABLE IF EXISTS tmp4_order_cust;
    CREATE TEMP TABLE tmp4_order_cust AS
    SELECT t.order_id, t.customer_id, t.product_id, t.order_date, t.order_amount,
           t.discount, t.payment_method, t.order_status, t.unit_price, t.quantity, t.tax_amt,
           t.product_name, t.category, t.brand, t.sell_price, t.stock_quantity,
           c.customer_name, c.gender, c.age, c.customer_level, c.province, c.city, c.address
    FROM tmp3_order_product t
    LEFT JOIN tmp1_cust_addr c ON t.customer_id = c.customer_id;

    -- 临时表5：tmp4 + 库存流水（product_id）
    DROP TABLE IF EXISTS tmp5_final;
    CREATE TEMP TABLE tmp5_final AS
    SELECT t.order_id, t.customer_id, t.product_id, t.order_date, t.order_amount,
           t.discount, t.payment_method, t.order_status, t.unit_price, t.quantity, t.tax_amt,
           t.product_name, t.category, t.brand, t.sell_price, t.stock_quantity,
           t.customer_name, t.gender, t.age, t.customer_level, t.province, t.city, t.address,
           i.warehouse_name, i.operate_date
    FROM tmp4_order_cust t
    LEFT JOIN src_product_inventory i ON t.product_id = i.product_id;

    -- 最终结果表（不含 tmp_ 前缀，过滤时不归入临时表）
    DROP TABLE IF EXISTS result_sale_report;
    CREATE TEMP TABLE result_sale_report AS
    SELECT t.customer_id,    t.customer_name,  t.gender,         t.age,           t.customer_level,
           t.province,        t.city,            t.address,
           t.product_id,      t.product_name,   t.category,       t.brand,         t.sell_price,    t.stock_quantity,
           t.warehouse_name,  t.operate_date,
           t.order_id,        t.order_date,     t.order_amount,   t.discount,      t.payment_method, t.order_status,
           t.unit_price,      t.quantity,        t.tax_amt
    FROM tmp5_final t
    ORDER BY t.order_date, t.order_id;

END;
$procedure$;
"""


# ---------------------------------------------------------------
# Fixture: 派生字段测试存储过程
# ---------------------------------------------------------------

DERIVED_CHAIN_SOURCE = """
CREATE OR REPLACE PROCEDURE p_test_derived_chain()
LANGUAGE plpgsql
AS $$
BEGIN
    DROP TABLE IF EXISTS tmp1_base;
    CREATE TEMP TABLE tmp1_base AS
    SELECT o.order_id, o.order_amount, o.discount,
           (o.order_amount - COALESCE(o.discount, 0)) AS net_amount
    FROM src_orders o;

    DROP TABLE IF EXISTS tmp2_calc;
    CREATE TEMP TABLE tmp2_calc AS
    SELECT t.order_id, t.net_amount,
           t.net_amount + 100 AS bonus_score
    FROM tmp1_base t;

    DROP TABLE IF EXISTS result_test;
    CREATE TEMP TABLE result_test AS
    SELECT t.order_id, t.net_amount, t.bonus_score
    FROM tmp2_calc t;
END;
$$;
"""


# ---------------------------------------------------------------
# 测试 1: 语句切分
# ---------------------------------------------------------------

def test_split_statements():
    stmts = split_statements(extract_body(P_GEN_SALE_REPORT_SOURCE))
    assert len(stmts) == 6, f"期望6条语句，实际{len(stmts)}条"
    create_as = [s for s in stmts if s["type"] == "create_as"]
    assert len(create_as) == 6, f"期望6条create_as，实际{len(create_as)}"
    tbl_names = [s["table"] for s in stmts]
    assert "tmp1_cust_addr" in tbl_names
    assert "tmp2_order_detail" in tbl_names
    assert "tmp3_order_product" in tbl_names
    assert "tmp4_order_cust" in tbl_names
    assert "tmp5_final" in tbl_names
    assert "result_sale_report" in tbl_names
    print("✅ test_split_statements PASSED")


# ---------------------------------------------------------------
# 测试 2: SELECT 列提取
# ---------------------------------------------------------------

def test_extract_select_columns():
    sql = "SELECT c.customer_id, c.customer_name, c.gender, c.age, c.customer_level FROM src_customer c"
    cols = extract_select_columns(sql)
    assert "customer_id" in cols
    assert "customer_name" in cols
    assert "gender" in cols

    sql2 = "SELECT o.order_amount - o.discount AS net_amount, o.order_id FROM src_orders o"
    cols2 = extract_select_columns(sql2)
    assert "net_amount" in cols2
    assert "order_id" in cols2
    print("✅ test_extract_select_columns PASSED")


# ---------------------------------------------------------------
# 测试 3: Schema Map 构建
# ---------------------------------------------------------------

def test_build_schema_map():
    stmts = split_statements(extract_body(P_GEN_SALE_REPORT_SOURCE))
    schema_map = build_schema_map(stmts, dialect="postgres")

    assert "tmp1_cust_addr" in schema_map
    tmp1_cols = schema_map["tmp1_cust_addr"].get_all_columns()
    assert tmp1_cols["customer_id"].src_table == "src_customer"
    assert tmp1_cols["province"].src_table == "src_customer_address"

    assert "tmp2_order_detail" in schema_map
    tmp2_cols = schema_map["tmp2_order_detail"].get_all_columns()
    assert tmp2_cols["order_id"].src_table == "src_orders"
    assert tmp2_cols["unit_price"].src_table == "src_orders_detail"

    assert "tmp3_order_product" in schema_map
    tmp3_cols = schema_map["tmp3_order_product"].get_all_columns()
    assert tmp3_cols["order_id"].is_tmp is True
    assert tmp3_cols["order_id"].src_table == "tmp2_order_detail"
    assert tmp3_cols["product_name"].src_table == "src_product"
    assert tmp3_cols["product_name"].is_tmp is False

    assert "result_sale_report" in schema_map
    result_cols = schema_map["result_sale_report"].get_all_columns()
    assert "customer_id" in result_cols
    assert "warehouse_name" in result_cols
    print("✅ test_build_schema_map PASSED")


# ---------------------------------------------------------------
# 测试 4: 血缘追溯
# ---------------------------------------------------------------

def test_trace_lineage():
    stmts = split_statements(extract_body(P_GEN_SALE_REPORT_SOURCE))
    schema_map = build_schema_map(stmts, dialect="postgres")
    lineage = trace_lineage(schema_map, "result_sale_report")

    lineage_map = {r["result_column"]: r for r in lineage}

    assert lineage_map["customer_name"]["src_table"] == "src_customer"
    assert lineage_map["province"]["src_table"] == "src_customer_address"
    assert lineage_map["order_id"]["src_table"] == "src_orders"
    assert lineage_map["unit_price"]["src_table"] == "src_orders_detail"
    assert lineage_map["product_name"]["src_table"] == "src_product"
    assert lineage_map["warehouse_name"]["src_table"] == "src_product_inventory"
    assert len(lineage) == 25, f"期望25个字段，实际{len(lineage)}"
    print("✅ test_trace_lineage PASSED")


# ---------------------------------------------------------------
# 测试 5: 端到端解析（无需真实数据库）
# ---------------------------------------------------------------

class MockConnector:
    def get_procedure_source(self, proc_name, schema):
        return P_GEN_SALE_REPORT_SOURCE


def test_end_to_end():
    parser = ProcedureLineageParser(db_type="postgresql", dialect="postgres")
    conn = MockConnector()
    result = parser.parse("p_gen_sale_report", "public", conn)

    assert result["procedure"] == "p_gen_sale_report"
    assert len(result["result_tables"]) == 1
    tbl = result["result_tables"][0]
    assert tbl["table"] == "result_sale_report"
    assert tbl["columns"] == 25

    src_tables = {r["src_table"] for r in tbl["lineage"]}
    assert "src_customer" in src_tables
    assert "src_customer_address" in src_tables
    assert "src_product" in src_tables
    assert "src_orders" in src_tables
    assert "src_orders_detail" in src_tables
    assert "src_product_inventory" in src_tables
    print("✅ test_end_to_end PASSED")


# ---------------------------------------------------------------
# 测试 6: 结果表过滤（不含 tmp_）
# ---------------------------------------------------------------

def test_result_table_filter():
    stmts = split_statements(extract_body(P_GEN_SALE_REPORT_SOURCE))
    schema_map = build_schema_map(stmts, dialect="postgres")

    all_tables = list(schema_map.keys())
    tmp_tables = [t for t in all_tables if re.match(r"tmp\d", t)]
    result_tables = [t for t in all_tables if not re.match(r"tmp\d", t)]

    assert len(tmp_tables) == 5, f"期望5个tmp_*临时表，实际{len(tmp_tables)}: {tmp_tables}"
    assert len(result_tables) == 1, f"期望1个结果表，实际{len(result_tables)}: {result_tables}"
    assert result_tables[0] == "result_sale_report"
    print("✅ test_result_table_filter PASSED")


# ---------------------------------------------------------------
# 测试 7: 派生字段血缘
# ---------------------------------------------------------------

def test_derived_columns():
    # CASE WHEN 码值转换
    sql1 = """SELECT CASE o.payment_method
                  WHEN 'credit_card' THEN '信用卡'
                  WHEN 'debit_card'  THEN '借记卡'
                  ELSE '其他'        END AS payment_label
              FROM src_orders o"""
    r1 = parse_select(sql1)
    assert "payment_label" in r1
    assert r1["payment_label"]["is_derived"] is True
    assert ("src_orders", "payment_method") in r1["payment_label"]["src_columns"]

    # 算术运算
    sql2 = """SELECT (o.order_amount - COALESCE(o.discount, 0)) AS net_amount
              FROM src_orders o"""
    r2 = parse_select(sql2)
    assert r2["net_amount"]["is_derived"] is True
    src_cols = r2["net_amount"]["src_columns"]
    assert ("src_orders", "order_amount") in src_cols
    assert ("src_orders", "discount") in src_cols

    # 简单列引用 → direct
    sql3 = """SELECT o.order_id, o.order_amount FROM src_orders o"""
    r3 = parse_select(sql3)
    assert r3["order_id"]["is_derived"] is False
    assert r3["order_amount"]["is_derived"] is False

    # 函数调用
    sql4 = """SELECT EXTRACT(YEAR FROM o.order_date) AS order_year
              FROM src_orders o"""
    r4 = parse_select(sql4)
    assert "order_year" in r4
    assert r4["order_year"]["is_derived"] is True
    assert ("src_orders", "order_date") in r4["order_year"]["src_columns"]

    # AS 别名透传：Alias(Column) → direct
    sql5 = """SELECT o.order_id AS order_no FROM src_orders o"""
    r5 = parse_select(sql5)
    assert r5["order_no"]["is_derived"] is False
    assert r5["order_no"]["src_table"] == "src_orders"

    print("✅ test_derived_columns PASSED")


# ---------------------------------------------------------------
# 测试 8: 派生属性跨临时表透传
# ---------------------------------------------------------------

def test_derived_chain_propagation():
    stmts = split_statements(extract_body(DERIVED_CHAIN_SOURCE))
    schema_map = build_schema_map(stmts, "postgres")

    # net_amount 在 tmp1_base 应该是 derived
    net_tmp1 = schema_map["tmp1_base"].get_column("net_amount")
    assert net_tmp1 is not None
    assert net_tmp1.direct is False, "tmp1_base.net_amount 应为派生字段"

    # net_amount 透传到 tmp2_calc 仍然是 derived
    net_tmp2 = schema_map["tmp2_calc"].get_column("net_amount")
    assert net_tmp2 is not None
    assert net_tmp2.direct is False, "tmp2_calc.net_amount 应透传派生属性"
    src_col_names = {sc[1] for sc in net_tmp2.src_columns}
    assert "order_amount" in src_col_names, f"期望追溯到 order_amount，实际 {src_col_names}"
    assert "discount" in src_col_names, f"期望追溯到 discount，实际 {src_col_names}"

    # result_test.net_amount 最终追溯到 src_orders
    lineage = trace_lineage(schema_map, "result_test")
    net_lineage = [r for r in lineage if r["result_column"] == "net_amount"]
    assert len(net_lineage) >= 2, f"net_amount 应有至少2条参与列血缘，实际{len(net_lineage)}"
    for r in net_lineage:
        assert r["lineage_type"] == "derived"
        assert r["src_table"] == "src_orders"
        assert r["chain"].startswith("src_orders"), f"链路应从 src_orders 开始: {r['chain']}"

    # bonus_score 是新派生表达式
    bonus_lineage = [r for r in lineage if r["result_column"] == "bonus_score"]
    assert len(bonus_lineage) >= 1
    for r in bonus_lineage:
        assert r["lineage_type"] == "derived"
        assert "src_orders" in r["src_table"], f"bonus_score 应追溯到 src_orders，实际 {r['src_table']}"

    print("✅ test_derived_chain_propagation PASSED")


# ---------------------------------------------------------------
# 运行所有测试
# ---------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 50)
    print("pg-lineage 测试套件")
    print("=" * 50)

    test_split_statements()
    test_extract_select_columns()
    test_build_schema_map()
    test_trace_lineage()
    test_result_table_filter()
    test_end_to_end()
    test_derived_columns()
    test_derived_chain_propagation()

    print("=" * 50)
    print("全部测试通过 ✅")
    print("=" * 50)
