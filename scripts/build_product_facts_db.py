"""
연금 Agent 과제 - 상품 팩트(product_master/class_fees/class_returns)를
SQLite 표로 적재

product_name/asset_type/risk_level/총보수/수익률처럼 "정답이 하나로 정해진
숫자·분류"를 텍스트 재검색 없이 바로 조회할 수 있게 하는 게 목적이다.
특히 "A상품이랑 B상품 총보수 비교해줘" 같은 비교 질의에서, 문서 텍스트
청크를 여러 개 긁어와 LLM에 던지는 대신 이 표에서 숫자만 뽑아 짧게
답할 수 있어 토큰을 크게 아낄 수 있다 (scripts/compare_products.py 참고).

기존 structured_store.db(표 전문검색용 tables/tables_fts)와 같은 DB 파일에
추가한다 - 근거 문서 표시(page 등)까지 한 곳에서 조회 가능하게.

사용법:
    python scripts/build_product_master.py     # product_master.json 생성
    python scripts/extract_class_fees.py        # class_fees.json 생성
    python scripts/extract_class_returns.py     # class_returns.json 생성
    python scripts/build_product_facts_db.py    # 위 3개를 SQLite로 적재
"""

import argparse
import json
import os
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "structured_store.db")

SCHEMA = """
DROP TABLE IF EXISTS product_master;
CREATE TABLE product_master (
    product_code TEXT PRIMARY KEY,
    product_name TEXT,
    product_name_confidence REAL,
    asset_type TEXT,
    asset_type_confidence REAL,
    risk_level INTEGER,
    risk_level_confidence REAL
);

DROP TABLE IF EXISTS class_fees;
CREATE TABLE class_fees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_code TEXT,
    class_code TEXT,
    sales_commission_desc TEXT,
    total_fee REAL,
    distribution_fee REAL,
    peer_avg_fee REAL,
    total_fee_and_cost REAL,
    cost_1y INTEGER,
    cost_2y INTEGER,
    cost_3y INTEGER,
    cost_5y INTEGER,
    cost_10y INTEGER,
    page INTEGER,
    confidence REAL
);
CREATE INDEX idx_class_fees_product ON class_fees(product_code);

DROP TABLE IF EXISTS class_returns;
CREATE TABLE class_returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_code TEXT,
    row_kind TEXT,
    class_code TEXT,
    inception_date TEXT,
    return_1y REAL,
    return_2y REAL,
    return_3y REAL,
    return_5y REAL,
    return_since_inception REAL,
    page INTEGER,
    confidence REAL
);
CREATE INDEX idx_class_returns_product ON class_returns(product_code);

-- 참고용: 운용전문인력 표의 "운용규모"는 이 상품 하나의 AUM이 아니라
-- 해당 운용역/운용사가 운용하는 전체 펀드 합산 규모다(is_product_aum=0
-- 고정). 6축 정답(product_master/class_fees/class_returns)과 섞이지
-- 않도록 별도 테이블로 분리한다.
DROP TABLE IF EXISTS manager_info;
CREATE TABLE manager_info (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_code TEXT,
    name TEXT,
    birth_year INTEGER,
    manager_fund_count INTEGER,
    manager_aum_billion_won REAL,
    is_product_aum INTEGER,
    career TEXT,
    page INTEGER,
    confidence REAL
);
CREATE INDEX idx_manager_info_product ON manager_info(product_code);
"""


def to_float(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def to_int(v):
    f = to_float(v)
    return int(f) if f is not None else None


def load_product_master(conn, path):
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    n = 0
    for r in records:
        conn.execute(
            """
            INSERT INTO product_master
                (product_code, product_name, product_name_confidence,
                 asset_type, asset_type_confidence, risk_level, risk_level_confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["product_code"],
                r["product_name"]["value"],
                r["product_name"]["confidence"],
                r["asset_type"]["value"],
                r["asset_type"]["confidence"],
                r["risk_level"]["value"],
                r["risk_level"]["confidence"],
            ),
        )
        n += 1
    return n


def load_class_fees(conn, path):
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    n = 0
    for r in records:
        cp = r.get("cost_projection_per_10m", {})
        conn.execute(
            """
            INSERT INTO class_fees
                (product_code, class_code, sales_commission_desc, total_fee,
                 distribution_fee, peer_avg_fee, total_fee_and_cost,
                 cost_1y, cost_2y, cost_3y, cost_5y, cost_10y, page, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["product_code"],
                r["class_code"],
                r["sales_commission_desc"],
                to_float(r["total_fee"]),
                to_float(r["distribution_fee"]),
                to_float(r["peer_avg_fee"]),
                to_float(r["total_fee_and_cost"]),
                to_int(cp.get("1y")),
                to_int(cp.get("2y")),
                to_int(cp.get("3y")),
                to_int(cp.get("5y")),
                to_int(cp.get("10y")),
                r["page"],
                r["confidence"],
            ),
        )
        n += 1
    return n


def load_class_returns(conn, path):
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    n = 0
    for r in records:
        v = r.get("values", {})
        conn.execute(
            """
            INSERT INTO class_returns
                (product_code, row_kind, class_code, inception_date,
                 return_1y, return_2y, return_3y, return_5y, return_since_inception,
                 page, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["product_code"],
                r["row_kind"],
                r["class_code"],
                r.get("inception_date"),
                to_float(v.get("1y")),
                to_float(v.get("2y")),
                to_float(v.get("3y")),
                to_float(v.get("5y")),
                to_float(v.get("since_inception")),
                r["page"],
                r["confidence"],
            ),
        )
        n += 1
    return n


def load_manager_info(conn, path):
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    n = 0
    for r in records:
        conn.execute(
            """
            INSERT INTO manager_info
                (product_code, name, birth_year, manager_fund_count,
                 manager_aum_billion_won, is_product_aum, career, page, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["product_code"],
                r["name"],
                r["birth_year"],
                r["manager_fund_count"],
                to_float(r["manager_aum_billion_won"]),
                0,
                r.get("career"),
                r["page"],
                r["confidence"],
            ),
        )
        n += 1
    return n


def main():
    parser = argparse.ArgumentParser(description="상품 팩트 3종을 SQLite로 적재")
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--product-master", default=os.path.join(REPO_ROOT, "product_master.json"))
    parser.add_argument("--class-fees", default=os.path.join(REPO_ROOT, "class_fees.json"))
    parser.add_argument("--class-returns", default=os.path.join(REPO_ROOT, "class_returns.json"))
    parser.add_argument("--manager-info", default=os.path.join(REPO_ROOT, "manager_info.json"))
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)

    n1 = load_product_master(conn, args.product_master)
    n2 = load_class_fees(conn, args.class_fees)
    n3 = load_class_returns(conn, args.class_returns)
    n4 = load_manager_info(conn, args.manager_info)

    conn.commit()
    conn.close()
    print(f"product_master {n1}건, class_fees {n2}건, class_returns {n3}건, manager_info(참고용) {n4}건 → {args.db}")


if __name__ == "__main__":
    main()
