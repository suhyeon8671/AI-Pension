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

# 답변에 펼쳐 보일 클래스 줄 수. 열몇 개를 다 늘어놓으면 고객이 못 읽는다.
MAX_CLASS_LINES = 5

# 질문이 무엇을 묻는지 - 겹치면 여러 개를 다 담는다(한 질문에 보수와
# 수익률을 같이 묻는 경우가 흔하다).
INTENT_KEYWORDS = {
    "fee": ("총보수", "보수", "수수료", "비용", "판매보수", "얼마나 떼", "비싸", "싸"),
    "return": ("수익률", "성과", "얼마나 벌", "수익", "실적", "올랐", "떨어졌"),
    # "작년에 얼마 벌었어?" - 연평균(누적)과 다른 값이라 따로 낸다.
    "yearly": ("작년", "재작년", "해마다", "연도별", "년도별", "매년", "해별",
               "올해", "지난해", "년에는", "년 수익", "연간 수익"),
    # 바닥글자 "위험"만으로 걸면 "위험요인"/"환율변동위험"/"신용위험"처럼
    # 투자설명서 본문(RAG)에만 있는 질적 위험 설명 질문까지 "위험등급
    # 몇 등급"으로 잘못 답하게 된다(실측: "주요 위험요인은 뭐야?"가
    # 위험등급 1등급이라고만 답함 - 220문항 테스트셋 20/64/192-198번과
    # 정면으로 겹치는 함정). "등급"이 붙어야 진짜 등급을 묻는 질문이다.
    "risk": ("위험등급", "등급"),
    "aum": ("설정액", "순자산", "규모", "자산총액", "얼마나 큰"),
    "cost_projection": ("비용예시", "1,000만원", "1000만원", "천만원", "투자하면"),
    "redemption": ("환매", "해지", "중도해지", "팔면", "빼면", "인출"),
    # "몇 시까지 신청하면", "돈 언제 들어와요" 같은 질문
    "timing": ("언제", "며칠", "몇 시", "시까지", "기준가", "지급", "들어와",
               "입금", "영업일", "청구하면", "신청하면"),
    # "매수"만 넣으면 "환매수수료"의 가운데 글자에 걸린다("환매수수료"
    # -> 환+매수+수료). 붙는 말까지 넣어 구분한다.
    "eligibility": ("가입할", "가입 가능", "가입자격", "살 수 있", "매수할", "매수 가능",
                    "담을 수", "연금저축계좌로", "IRP로", "IRP에서"),
}


def detect_intents(question):
    """질문에서 알아본 의도 목록. 하나도 못 알아보면 빈 리스트.

    예전엔 못 알아보면 ["fee", "return"]로 조용히 넘겨짚었다. 그러면
    api/server.py가 "이 질문은 구조화 DB로 답할 수 있다"고 잘못 판단해서,
    "투자목적이 뭐야?"/"운용사가 어디야?"/"위험요인이 뭐야?"/"원금보장
    상품이야?"처럼 실제로는 투자설명서 본문(RAG)에만 있는 질문에도
    총보수·수익률 숫자를 답으로 내보냈다(실측: 8개 질문으로 재현 확인,
    전부 총보수 안내로 잘못 답함). 여기서 못 알아보면 빈 리스트를 그대로
    돌려줘야, server.py가 "구조화 DB로 답할 의도가 없다"고 보고 RAG로
    넘긴다. product_facts() 자신은 intents가 비어 오면 여전히 fee+return을
    기본값으로 쓴다(CLI로 이 모듈만 단독 호출할 때의 편의 기본값) - 그건
    이 함수가 아니라 product_facts()의 몫이라 그대로 둔다."""
    q = (question or "").replace(" ", "")
    return [k for k, kws in INTENT_KEYWORDS.items()
            if any(w.replace(" ", "") in q for w in kws)]


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


def _meaning_for(conn, code):
    """클래스 코드 -> 뜻. 코드를 그대로 답에 쓰면 고객이 못 알아본다."""
    return {r["class_code"]: dict(r) for r in conn.execute(
        "SELECT * FROM class_meaning WHERE product_code = ?", (code,))}


def _charges_for(conn, code):
    return {r["class_code"]: dict(r) for r in conn.execute(
        "SELECT * FROM class_charges WHERE product_code = ?", (code,))}


def _label(code, meaning):
    """답변에 쓸 클래스 이름. 뜻을 알면 말로, 모르면 코드 그대로."""
    m = meaning.get(code)
    if m and m.get("description"):
        # 종류형이 아닌 펀드는 클래스 코드 자리에 "투자신탁"처럼 형태
        # 이름이 들어가 있다(KR5123365001). 그걸 코드처럼 괄호에 달면
        # "클래스 구분 없는 단일 펀드 (투자신탁)"이 되어 되레 클래스가
        # 있는 것처럼 보인다. 이름표에 수수료방식도 판매경로도 없는
        # 행이 그 경우다.
        if not m.get("channel") and not m.get("fee_type"):
            return m["description"]
        return f"{m['description']} ({code})"
    return f"{code} 클래스"


def _fee_lines(fees, meaning):
    """보수를 '범위 + 조건별'로 적는다.

    대표 클래스를 정해 숫자 하나만 말할 수는 없다. 상품 100개의 클래스
    구성이 70가지로 제각각이라 어떤 코드를 대표로 잡아도 최소 30개
    상품에는 그게 없고, 한 펀드 안에서 총보수가 최대 1.5%p(0.7% <-> 2.2%)
    까지 벌어져서 아무 클래스나 집으면 틀린 답이 된다.

    그래서 일반 고객이 가입할 수 있는 클래스의 범위를 먼저 말하고,
    조건별로 펼친다. 기관·고액·랩 전용은 살 수가 없으므로 뺀다 - 안 빼면
    교보악사 Tomorrow장기우량처럼 싼 순서 넷이 전부 전용 클래스인 상품에서
    "제일 싼 게 0.1195%"라고 살 수도 없는 걸 안내하게 된다."""
    priced = [f for f in fees if f.get("total_fee") is not None]
    if not priced:
        return ["  보수: 총보수 값을 찾지 못했습니다."], []

    retail, restricted, unknown = [], [], []
    for f in priced:
        m = meaning.get(f["class_code"])
        if m is None:
            unknown.append(f)
        elif m.get("retail"):
            retail.append(f)
        else:
            restricted.append(f)

    shown = sorted(retail or unknown, key=lambda f: f["total_fee"])
    # 표기만 다른 같은 클래스가 두 줄로 나오는 걸 막는다(C-E / CE).
    # 코드는 다르지만 뜻도 값도 같으면 고객에겐 같은 것이다.
    deduped, seen = [], set()
    for f in shown:
        m = meaning.get(f["class_code"]) or {}
        key = (m.get("description"), f["total_fee"], f.get("distribution_fee"))
        if m.get("description") and key in seen:
            continue
        seen.add(key)
        deduped.append(f)
    shown = deduped
    lo, hi = shown[0]["total_fee"], shown[-1]["total_fee"]

    lines = []
    if lo == hi:
        lines.append(f"  [총보수] 연 {lo}%")
    else:
        lines.append(f"  [총보수] 연 {lo}% ~ {hi}% — 가입 방법에 따라 다릅니다")

    # 클래스가 열몇 개씩 되는 상품이 흔한데 전부 나열하면 고객이 못 읽는다.
    # 연금 계좌로 살 수 있는 클래스를 앞세우고, 나머지는 제일 싼 것과
    # 제일 비싼 것만 보인다(범위가 어디서 오는지는 보여야 하므로).
    # 다만 범위의 양 끝은 반드시 보인다. 위에서 "연 1.09% ~ 2.07%"라고
    # 말해 놓고 2.07%짜리 줄이 없으면 그 숫자가 어디서 나왔는지 알 수 없다.
    pension = [f for f in shown
               if (meaning.get(f["class_code"]) or {}).get("account_type")]
    picked = pension or shown
    if len(picked) > MAX_CLASS_LINES:
        picked = picked[:MAX_CLASS_LINES - 1] + [picked[-1]]
    for edge in (shown[-1], shown[0]):
        if edge not in picked:
            picked = [edge] + picked if edge is shown[0] else picked + [edge]
    # "총보수 얼마?"처럼 클래스를 안 짚은 질문은 대개 가장 기본형(온라인·
    # 연금 같은 조건 없이 그냥 "A"/"C")을 궁금해하는 것이다(KR5147430065
    # 실측: PROD-08 검증 실패 - 연금 계좌 클래스만 앞세우다 보니 정작
    # 가장 기본적인 "A" 클래스(0.443%)가 범위 설명에만 녹아들고 숫자로는
    # 안 나왔다). 연금 클래스에 밀려도 기본형 A/C는 항상 보인다.
    base = next((f for f in shown if f["class_code"] in ("A", "C")), None)
    if base is not None and base not in picked:
        picked = picked + [base]
    picked = sorted(picked, key=lambda f: f["total_fee"])

    for f in picked:
        bits = [f"    - {_label(f['class_code'], meaning)}: {f['total_fee']}%"]
        if f.get("distribution_fee") is not None:
            bits.append(f"판매보수 {f['distribution_fee']}%")
        if f.get("sales_commission_desc") and f["sales_commission_desc"] != "-":
            bits.append(f"판매수수료 {f['sales_commission_desc']}")
        lines.append(", ".join(bits))
    rest = len(shown) - len(picked)
    if rest > 0:
        lines.append(f"    (이 외 {rest}개 클래스가 있으며 모두 위 범위 안입니다)")

    if restricted:
        names = ", ".join(sorted({
            meaning[f["class_code"]]["description"] for f in restricted}))
        lines.append(f"    ※ 이 펀드에는 {names} 클래스도 있으나 일반 개인 "
                     "고객은 가입할 수 없어 위 범위에서 제외했습니다.")
    if retail and unknown:
        lines.append(f"    ※ 가입 조건을 확인하지 못한 클래스 "
                     f"{', '.join(f['class_code'] for f in unknown)}는 제외했습니다.")
    elif unknown and not retail:
        lines.append("    ※ 클래스별 가입 조건을 문서에서 확인하지 못했습니다.")
    return lines, shown


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

        meaning = _meaning_for(conn, code)
        charges = _charges_for(conn, code)

        if "fee" in intents or "cost_projection" in intents:
            fees = _classes_for(conn, code, class_code)
            if not fees:
                lines.append("  보수: 해당 클래스 정보를 찾지 못했습니다.")
            else:
                fee_lines, shown = _fee_lines(fees, meaning)
                lines.extend(fee_lines)
                as_of = next((f.get("as_of") for f in fees if f.get("as_of")), None)
                if as_of:
                    lines.append(f"    (작성 기준일 {as_of})")
                for f in shown:
                    ev.append({"table": "class_fees", "product_code": code,
                               "class_code": f["class_code"], "page": f.get("page")})

        if "redemption" in intents:
            # 값을 문장 그대로 담아 둔 이유가 여기서 드러난다. "90일미만
            # 이익금의 30%"만 말하면 틀린 답이 되는 경우가 있다(뒤에
            # "다만 ... 부과하지 않음"이 붙는다).
            got = [(cc, c) for cc, c in sorted(charges.items())
                   if c.get("redemption_fee")]
            note = conn.execute(
                "SELECT redemption_note FROM product_charges WHERE product_code = ?",
                (code,)).fetchone()
            if not got and note and note["redemption_note"]:
                # 클래스별 표가 없어도 펀드 전체에 대해 적어 둔 문장이 있으면
                # 그게 답이다. "모릅니다"로 답할 이유가 없다.
                lines.append(f"  [환매수수료] {note['redemption_note']}.")
                ev.append({"table": "product_charges", "product_code": code})
            elif not got:
                lines.append("  [환매수수료] 문서에서 확인하지 못했습니다.")
            elif len({c["redemption_fee"] for _cc, c in got}) == 1:
                lines.append(f"  [환매수수료] {got[0][1]['redemption_fee']}")
                ev.append({"table": "class_charges", "product_code": code,
                           "page": got[0][1].get("page")})
            else:
                lines.append("  [환매수수료] 클래스에 따라 다릅니다")
                for cc, c in got:
                    lines.append(f"    - {_label(cc, meaning)}: {c['redemption_fee']}")
                    ev.append({"table": "class_charges", "product_code": code,
                               "class_code": cc, "page": c.get("page")})

        if "yearly" in intents:
            # 연평균은 여러 해를 묶은 값이라 "작년 성과"와 다르다. 해마다의
            # 값은 따로 실려 있고, 몇 년 몇 월 구간인지도 같이 말해야 한다
            # (문서마다 회계연도 시작이 다르다).
            yr = list(conn.execute(
                "SELECT * FROM yearly_returns WHERE product_code = ? "
                "AND row_kind = 'class_return'"
                + (" AND class_code = ?" if class_code else "")
                + " ORDER BY class_code, year_rank",
                [code] + ([class_code] if class_code else [])))
            if not yr:
                lines.append("  [연도별 수익률] 문서에서 확인하지 못했습니다.")
            else:
                by_class = {}
                for r in yr:
                    by_class.setdefault(r["class_code"], []).append(r)
                # 클래스가 여럿이면 다 늘어놓지 않는다. 연금 계좌용을
                # 앞세우고 하나만 보인다 - 해마다 값은 클래스별로 소수점
                # 아래만 다르다.
                pick = next((c for c in by_class
                             if (meaning.get(c) or {}).get("account_type")
                             and (meaning.get(c) or {}).get("retail")),
                            next(iter(by_class)))
                lines.append(f"  [연도별 수익률] {_label(pick, meaning)} 기준")
                for r in by_class[pick]:
                    lines.append(f"    - 최근 {r['year_rank']}년차"
                                 f"({r['period']}): {r['return_pct']}%")
                    ev.append({"table": "yearly_returns", "product_code": code,
                               "class_code": pick, "page": r["page"]})
                if len(by_class) > 1:
                    lines.append(f"    (다른 클래스 {len(by_class) - 1}개도 "
                                 "있으며 소수점 아래에서만 차이가 납니다)")

        if "timing" in intents:
            rules = {r["kind"]: dict(r) for r in conn.execute(
                "SELECT * FROM trade_rules WHERE product_code = ?", (code,))}
            if not rules:
                lines.append("  [매입·환매 기준가격] 문서에서 확인하지 못했습니다.")
            for kind, title in (("매입기준가", "매입 시 기준가격"),
                                ("환매기준가", "환매 시 기준가격·지급시기")):
                r = rules.get(kind)
                if not r:
                    continue
                lines.append(f"  [{title}] {r['text']}")
                ev.append({"table": "trade_rules", "product_code": code,
                           "kind": kind, "page": r.get("page")})

        if "eligibility" in intents:
            got = [(cc, c) for cc, c in sorted(charges.items())
                   if c.get("eligibility")]
            if not got:
                # 가입자격 열은 문서 27개에만 있다. 없으면 이름표의 계좌
                # 종류로 답한다 - "연금저축 · 온라인"이면 연금저축 계좌로
                # 살 수 있다는 뜻이고, 그게 질문에 대한 답이다.
                by_account = {}
                for cc, m in sorted(meaning.items()):
                    if m.get("account_type") and m.get("retail"):
                        by_account.setdefault(
                            m["description"].split(" · ")[0], []).append(cc)
                if by_account:
                    lines.append("  [가입 가능한 계좌]")
                    for account, ccs in by_account.items():
                        lines.append(f"    - {account}: {', '.join(ccs)} 클래스")
                        ev.append({"table": "class_meaning", "product_code": code,
                                   "class_code": ccs[0]})
                    if not any(a for a in by_account
                               if "연금" in a):
                        lines.append("    ※ 연금 계좌 전용 클래스는 없습니다.")
                else:
                    lines.append("  [가입자격] 이 펀드에는 연금저축·퇴직연금 전용 "
                                 "클래스가 문서에 표시되어 있지 않습니다.")
            else:
                lines.append("  [가입자격]")
                for cc, c in got:
                    lines.append(f"    - {_label(cc, meaning)}: {c['eligibility']}")
                    ev.append({"table": "class_charges", "product_code": code,
                               "class_code": cc, "page": c.get("page")})

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
