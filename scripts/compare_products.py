"""
연금 Agent 과제 - 상품 비교 질의 처리 (토큰 절약용 구조화 조회)

"OO상품이랑 XX상품 총보수 비교해줘" 같은 질의는 이미 확실한 숫자가
product_master/class_fees/class_returns 표(structured_store.db)에 있는데,
이걸 semantic_search로 텍스트 청크를 여러 개 긁어와 LLM에 던지면 (1) 토큰을
많이 쓰고 (2) 정작 필요한 숫자가 청크 경계에 걸려 잘릴 수도 있다.

비교 대상 상품(코드 또는 이름)이 질의에 2개 이상 있으면, DB에서 필요한
필드만 직접 조회해서 짧은 텍스트로 반환한다. 클래스를 지정하지 않으면
그 상품에서 confidence가 가장 높은 클래스 1개만 대표로 보여준다(전체
클래스를 다 보여주면 오히려 길어져서 토큰 절약 취지에 안 맞음).

사용법(CLI, 수동 점검용):
    python scripts/compare_products.py --codes KR5127420034,KR5127420039
"""

import argparse
import re
import sqlite3

# 예전엔 router에서 상품코드 정규식을 가져왔는데, router가 벡터 검색
# (chromadb)을 끌어와서 벡터 스토어가 없는 환경에선 이 모듈까지 못 쓰게
# 된다. 구조화 DB 조회는 벡터 검색과 무관하므로 끊어 둔다.
from product_lookup import PRODUCT_CODE_RE  # noqa: E402
from build_product_facts_db import DEFAULT_DB_PATH

COMPARISON_KEYWORDS = ["비교", "차이", "어느", "어디가", "더 낮", "더 높", "vs", "대비"]


def extract_product_codes(query: str) -> list:
    return list(dict.fromkeys(PRODUCT_CODE_RE.findall(query)))


def is_comparison_query(query: str, product_codes: list) -> bool:
    if len(product_codes) >= 2:
        return True
    return len(product_codes) >= 1 and any(k in query for k in COMPARISON_KEYWORDS)


def _fetch_product(conn, code):
    row = conn.execute(
        "SELECT product_code, product_name, asset_type, risk_level FROM product_master WHERE product_code = ?",
        (code,),
    ).fetchone()
    return row


def _fetch_best_class_fee(conn, code, class_code=None):
    sql = "SELECT class_code, total_fee, distribution_fee, peer_avg_fee, total_fee_and_cost, confidence FROM class_fees WHERE product_code = ?"
    params = [code]
    if class_code:
        sql += " AND class_code = ?"
        params.append(class_code)
    sql += " ORDER BY confidence DESC, total_fee ASC LIMIT 1"
    return conn.execute(sql, params).fetchone()


# 수익률 값이 하나라도 있는 행만 쓴다. 예전엔 confidence만 보고 골라서
# 값이 전부 비어 있는 행(클래스만 있고 수익률은 안 실린 행)이 뽑혔고,
# 답변에 "최근1년 수익률(C1클래스) None%"가 그대로 나갔다.
_RETURN_HAS_VALUE = ("(return_1y IS NOT NULL OR return_3y IS NOT NULL "
                     "OR return_since_inception IS NOT NULL)")


def _fetch_best_class_return(conn, code, class_code=None):
    sql = (
        "SELECT class_code, return_1y, return_3y, return_since_inception, confidence "
        "FROM class_returns WHERE product_code = ? AND row_kind = 'class_return' "
        "AND " + _RETURN_HAS_VALUE
    )
    params = [code]
    if class_code:
        sql += " AND class_code = ?"
        params.append(class_code)
    sql += " ORDER BY confidence DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def _fee_classes(conn, code):
    return {r[0] for r in conn.execute(
        "SELECT class_code FROM class_fees "
        "WHERE product_code = ? AND total_fee IS NOT NULL", (code,))}


def _return_classes(conn, code):
    return {r[0] for r in conn.execute(
        "SELECT class_code FROM class_returns WHERE product_code = ? "
        "AND row_kind = 'class_return' AND " + _RETURN_HAS_VALUE, (code,))}


def _rep_class_key(c):
    """대표로 보여줄 클래스 고르는 순서. A(창구 판매) -> C -> 나머지."""
    c = c or ""
    return (0 if c == "A" else 1 if c == "C" else 2, len(c), c)


def _common_class(conn, codes, fields):
    """비교 대상 전부가 갖고 있는 클래스 하나. 없으면 None.

    클래스마다 보수가 크게 달라서(A 1.47% vs A-e 1.12%), 상품마다 다른
    클래스를 대표로 뽑아 나란히 놓으면 상품 차이가 아니라 클래스 차이를
    보여주게 된다. 가능하면 같은 클래스끼리 견준다."""
    common = None
    for code in codes:
        s = _fee_classes(conn, code) if "fee" in fields else None
        if "return" in fields:
            r = _return_classes(conn, code)
            s = r if s is None else (s & r)
        if not s:
            return None
        common = s if common is None else (common & s)
        if not common:
            return None
    return min(common, key=_rep_class_key) if common else None


def compare_products(product_codes, db_path=DEFAULT_DB_PATH, fields=None):
    """product_codes: ['KR...', 'KR...'] (2개 이상)
    fields: {"fee", "return", "risk"} 중 관심 있는 것만 (None이면 전부)
    반환: (요약 텍스트, 근거 목록)
    """
    fields = fields or {"fee", "return", "risk"}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    lines = []
    evidence = []
    common = _common_class(conn, product_codes, fields)
    used_classes = set()
    for code in product_codes:
        product = _fetch_product(conn, code)
        if not product:
            lines.append(f"[{code}] 상품 정보를 찾지 못함")
            continue

        name = product["product_name"] or code
        parts = [f"[{code}] {name}"]
        if "risk" in fields and product["risk_level"] is not None:
            parts.append(f"위험등급 {product['risk_level']}등급")
        if product["asset_type"]:
            parts.append(f"({product['asset_type']})")

        # fee/return을 각각 독립적으로 "가장 좋은 클래스"를 고르면 서로 다른
        # 클래스가 섞여서(총보수는 A-E클래스, 수익률은 C클래스처럼) 비교
        # 결과가 혼란스러워진다. 수익률 표는 보통 대표 클래스 하나만 실어서
        # (총보수 표는 클래스별로 다 있는 것과 달리) 더 희소하므로, 수익률이
        # 있는 클래스를 먼저 정하고 총보수를 그 클래스에 맞춰 조회한다.
        rep_class = common
        ret = None
        if "return" in fields:
            ret = (_fetch_best_class_return(conn, code, class_code=rep_class)
                   if rep_class else None) or _fetch_best_class_return(conn, code)
            if ret:
                rep_class = rep_class or ret["class_code"]

        if "fee" in fields:
            fee = _fetch_best_class_fee(conn, code, class_code=rep_class) or _fetch_best_class_fee(conn, code)
            if fee:
                rep_class = rep_class or fee["class_code"]
                used_classes.add(fee["class_code"])
                parts.append(f"총보수({fee['class_code']}클래스) {fee['total_fee']}%")
                evidence.append({"product_code": code, "type": "class_fees", "class_code": fee["class_code"]})

        if ret:
            used_classes.add(ret["class_code"])
            got = [(lbl, ret[col]) for lbl, col in
                   (("최근1년", "return_1y"), ("최근3년", "return_3y"),
                    ("설정후", "return_since_inception"))
                   if ret[col] is not None]
            if got:
                parts.append(f"수익률({ret['class_code']}클래스) "
                             + ", ".join(f"{lbl} {v}%" for lbl, v in got))
            evidence.append({"product_code": code, "type": "class_returns", "class_code": ret["class_code"]})

        lines.append(" | ".join(parts))

    # 같은 클래스로 못 맞췄으면 그 사실을 말한다. 클래스가 다르면 숫자를
    # 그대로 견주는 게 틀린 비교가 되기 때문이다.
    if len(used_classes) > 1:
        lines.append("※ 상품마다 실린 클래스가 달라 서로 다른 클래스의 값입니다"
                     f"({', '.join(sorted(used_classes))}). 클래스에 따라 보수가"
                     " 달라지므로 같은 클래스끼리 비교해야 정확합니다.")

    conn.close()
    return "\n".join(lines), evidence


def main():
    parser = argparse.ArgumentParser(description="상품 비교 조회 수동 점검 CLI")
    parser.add_argument("--codes", help="쉼표로 구분한 상품코드 (예: KR..,KR..)")
    parser.add_argument("--query", help="자연어 질의에서 상품코드를 직접 추출")
    args = parser.parse_args()

    if args.query:
        codes = extract_product_codes(args.query)
        print(f"추출된 상품코드: {codes}")
    else:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]

    summary, evidence = compare_products(codes)
    print(summary)
    print("\n근거:", evidence)


if __name__ == "__main__":
    main()
