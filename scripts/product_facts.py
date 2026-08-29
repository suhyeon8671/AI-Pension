"""상품 하나에 대한 정량 질문을 구조화 DB에서 바로 답한다.

지금까지 구조화 DB(class_fees/class_returns/fund_aum)를 쓰는 경로는
"상품 2개 이상 비교" 하나뿐이었다. 그래서 "이 펀드 총보수 얼마야?" 같은
가장 흔한 질문이 텍스트 청크 검색으로 빠지고, 정확히 뽑아 둔 숫자를
못 쓰고 있었다. 이 모듈이 그 경로를 만든다.

원칙:
- 값은 DB에서 그대로 가져온다(해석하지 않는다).
- 어느 클래스·어느 기준일의 값인지 항상 같이 낸다. 보수·수익률은 시점과
  클래스에 따라 달라지는 값이라 그것 없이 숫자만 말하면 틀린 답이 된다.
- 없는 값은 없다고 말한다(추정하지 않는다).
"""

import os
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "structured_store.db")

# 질문이 무엇을 묻는지 - 겹치면 여러 개를 다 담는다(한 질문에 보수와
# 수익률을 같이 묻는 경우가 흔하다).
INTENT_KEYWORDS = {
    "fee": ("총보수", "보수", "수수료", "비용", "판매보수", "얼마나 떼", "비싸", "싸"),
    "return": ("수익률", "성과", "얼마나 벌", "수익", "실적", "올랐", "떨어졌"),
    "risk": ("위험등급", "위험", "등급"),
    "aum": ("설정액", "순자산", "규모", "자산총액", "얼마나 큰"),
    "cost_projection": ("비용예시", "1,000만원", "1000만원", "천만원", "투자하면"),
}


def detect_intents(question):
    q = (question or "").replace(" ", "")
    found = [k for k, kws in INTENT_KEYWORDS.items()
             if any(w.replace(" ", "") in q for w in kws)]
    return found or ["fee", "return"]


def _classes_for(conn, code, class_code=None):
    sql = ("SELECT * FROM class_fees WHERE product_code = ?"
           + (" AND class_code = ?" if class_code else "")
           + " ORDER BY class_code")
    params = [code] + ([class_code] if class_code else [])
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _returns_for(conn, code, class_code=None):
    sql = ("SELECT * FROM class_returns WHERE product_code = ? "
           "AND row_kind = 'class_return'"
           + (" AND class_code = ?" if class_code else "")
           + " ORDER BY class_code")
    params = [code] + ([class_code] if class_code else [])
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _benchmark_for(conn, code):
    r = conn.execute(
        "SELECT * FROM class_returns WHERE product_code = ? AND row_kind = 'benchmark' LIMIT 1",
        (code,)).fetchone()
    return dict(r) if r else None


def product_facts(code, class_code=None, intents=None, db_path=DEFAULT_DB_PATH):
    """상품 하나의 사실을 모아 (사람이 읽을 요약, 근거 목록)로 돌려준다."""
    intents = intents or ["fee", "return"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        prod = conn.execute(
            "SELECT * FROM product_master WHERE product_code = ?", (code,)
        ).fetchone()
        if not prod:
            return f"[{code}] 상품 정보를 찾지 못했습니다.", []
        prod = dict(prod)

        lines = [f"■ {prod.get('product_name') or code} ({code})"]
        ev = []
        head = []
        if prod.get("asset_type"):
            head.append(f"분류 {prod['asset_type']}")
        if prod.get("risk_level") is not None:
            head.append(f"위험등급 {prod['risk_level']}등급")
        if head:
            lines.append("  " + " | ".join(head))

        if "risk" in intents and prod.get("risk_level") is not None:
            ev.append({"table": "product_master", "product_code": code})

        if "fee" in intents or "cost_projection" in intents:
            fees = _classes_for(conn, code, class_code)
            if not fees:
                lines.append("  보수: 해당 클래스 정보를 찾지 못했습니다.")
            else:
                lines.append(f"  [보수] 클래스 {len(fees)}개")
                for f in fees:
                    bits = [f"    - {f['class_code']}: 총보수 {f['total_fee']}%"]
                    if f.get("distribution_fee") not in (None, ""):
                        bits.append(f"판매보수 {f['distribution_fee']}%")
                    if f.get("total_fee_and_cost") not in (None, ""):
                        bits.append(f"총보수·비용 {f['total_fee_and_cost']}%")
                    if f.get("sales_commission_desc"):
                        bits.append(f"판매수수료 {f['sales_commission_desc']}")
                    lines.append(", ".join(bits))
                    ev.append({"table": "class_fees", "product_code": code,
                               "class_code": f["class_code"], "page": f.get("page")})

        if "return" in intents:
            rets = _returns_for(conn, code, class_code)
            if not rets:
                lines.append("  수익률: 해당 클래스 정보를 찾지 못했습니다.")
            else:
                lines.append(f"  [수익률(연평균, %)] 클래스 {len(rets)}개")
                for r in rets:
                    got = [(lbl, r.get(col)) for lbl, col in
                           (("1년", "return_1y"), ("2년", "return_2y"),
                            ("3년", "return_3y"), ("5년", "return_5y"),
                            ("설정후", "return_since_inception"))
                           if r.get(col) not in (None, "")]
                    if not got:
                        continue
                    txt = ", ".join(f"{lbl} {v}" for lbl, v in got)
                    lines.append(f"    - {r['class_code']}: {txt}")
                    ev.append({"table": "class_returns", "product_code": code,
                               "class_code": r["class_code"], "page": r.get("page")})
                bm = _benchmark_for(conn, code)
                if bm:
                    got = [(lbl, bm.get(col)) for lbl, col in
                           (("1년", "return_1y"), ("3년", "return_3y"),
                            ("설정후", "return_since_inception"))
                           if bm.get(col) not in (None, "")]
                    if got:
                        lines.append("    - 비교지수: "
                                     + ", ".join(f"{lbl} {v}" for lbl, v in got))
                        ev.append({"table": "class_returns", "product_code": code,
                                   "row_kind": "benchmark", "page": bm.get("page")})

        if "aum" in intents:
            a = conn.execute(
                "SELECT * FROM fund_aum WHERE product_code = ?", (code,)).fetchone()
            if a:
                a = dict(a)
                lines.append(f"  [규모] 순자산 {a.get('net_asset_latest')} "
                             f"{a.get('unit') or ''}")
                ev.append({"table": "fund_aum", "product_code": code,
                           "page": a.get("page")})
            else:
                lines.append("  규모: 정보를 찾지 못했습니다.")

        return "\n".join(lines), ev
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    from product_lookup import find_products, find_class_code
    q = " ".join(sys.argv[1:])
    hits = find_products(q)
    if not hits:
        print("상품을 찾지 못했습니다.")
    else:
        cc = find_class_code(q)
        ints = detect_intents(q)
        print(f"(의도: {ints} / 클래스: {cc})\n")
        for code, name, _ in hits[:2]:
            s, ev = product_facts(code, cc, ints)
            print(s)
            print("  근거:", ev[:3], "...\n")
