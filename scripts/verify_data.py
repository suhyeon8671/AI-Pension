"""구조화 DB의 값이 서로 앞뒤가 맞는지 본다.

왜 질의응답 검증만으로는 모자라나
--------------------------------
답변 검증 세트(eval/questions.jsonl)는 "답에 이 글자가 있나"를 본다.
값이 맞는지는 못 본다. 실제로 오늘 잡은 결함들은 전부 그 세트를 통과한
상태였다.

- class_returns의 연평균 칸에 연도별 값이 들어 있었다(4개 상품).
  "최근 3년 -31.08%"라고 답했는데 진짜 연평균 3년은 -10.60%였다.
- 고액 전용 클래스가 "일반 가입 가능"으로 표시됐다(KR515302022M CI).
- 선취판매수수료 칸에 클래스 이름표가 들어가 있었다(10건).

세 가지 다 답변 검증이 아니라 데이터끼리 대조해서 찾았다. 질문을 100개로
늘려도 이런 건 못 잡는다 - 답이 그럴듯하게 나오기 때문이다.

여기서 보는 것은 두 갈래다.

1. 한 표 안에서 앞뒤가 맞나 (총보수 >= 판매보수, 비용예시가 늘어나나)
2. 표끼리 앞뒤가 맞나 (연평균이 연도별과 같으면 뒤섞인 것,
   이름표의 "일반 가입 가능"이 가입자격 원문과 어긋나나)

실행:
    python3 scripts/verify_data.py
    python3 scripts/verify_data.py --show 20   # 사례를 더 보기
"""

import argparse
import collections
import os
import re
import sqlite3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_ROOT, "structured_store.db")

# 보수율이 이보다 크면 값을 잘못 읽은 것이다(퍼센트인데 다른 칸을 집었거나).
MAX_FEE_PCT = 10.0
# 작성기준일이 이 범위 밖이면 엉뚱한 날짜를 집은 것이다.
AS_OF_MIN, AS_OF_MAX = "2015-01-01", "2027-12-31"
# 값이 같다고 볼 오차
EPS = 0.005


class Report:
    def __init__(self):
        self.checks = []

    def add(self, name, bad, total, samples, note="", info=False):
        # info=True는 "고칠 것"이 아니라 "알고 있어야 할 것"이다. 0이 되는
        # 게 목표가 아니라서 실패로 세지 않는다.
        self.checks.append((name, bad, total, samples, note, info))

    def show(self, limit):
        worst = 0
        for name, bad, total, samples, note, info in self.checks:
            mark = ".. " if info else "OK " if not bad else "!! "
            print(f"{mark}{name}: {bad}건" + (f" / {total}건 검사" if total else ""))
            if note and bad:
                print(f"      {note}")
            for s in samples[:limit]:
                print(f"      {s}")
            if not info:
                worst = max(worst, bad)
        return worst


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params)]


def check_fee_internal(conn, rep):
    """한 클래스 안에서 보수 값들이 앞뒤가 맞나."""
    bad_order, bad_range, bad_cost = [], [], []
    rows = _rows(conn, "SELECT * FROM class_fees")
    for r in rows:
        tf, df, tfc = r["total_fee"], r["distribution_fee"], r["total_fee_and_cost"]
        if tf is not None and tf > MAX_FEE_PCT:
            bad_range.append(f"{r['product_code']} {r['class_code']}: 총보수 {tf}%")
        if tf is not None and df is not None and df - tf > EPS:
            bad_order.append(
                f"{r['product_code']} {r['class_code']}: 판매보수 {df} > 총보수 {tf}")
        if tf is not None and tfc is not None and tf - tfc > EPS:
            bad_order.append(
                f"{r['product_code']} {r['class_code']}: 총보수 {tf} > 총보수·비용 {tfc}")
        costs = [r[c] for c in ("cost_1y", "cost_2y", "cost_3y", "cost_5y", "cost_10y")]
        seen = [c for c in costs if c is not None]
        for a, b in zip(seen, seen[1:]):
            if b < a - EPS:
                bad_cost.append(
                    f"{r['product_code']} {r['class_code']}: 비용예시 {seen}")
                break
    rep.add("총보수가 판매보수보다 작음 / 총보수·비용보다 큼", len(bad_order),
            len(rows), bad_order)
    rep.add(f"보수율이 {MAX_FEE_PCT}%를 넘음", len(bad_range), len(rows), bad_range)
    rep.add("비용예시가 기간이 길수록 줄어듦", len(bad_cost), len(rows), bad_cost,
            "1,000만원을 오래 넣을수록 비용이 줄 수는 없다")


def check_avg_vs_yearly(conn, rep):
    """연평균 칸에 연도별 값이 들어갔나.

    연평균 2년/3년은 여러 해를 묶어 연 단위로 환산한 값이고, 연도별
    2년차/3년차는 그 해 하나의 값이다. 우연히 같을 수는 있어도 여러
    클래스가 한꺼번에 같으면 표를 잘못 읽은 것이다."""
    yearly = collections.defaultdict(dict)
    for r in _rows(conn, "SELECT * FROM yearly_returns WHERE row_kind='class_return'"):
        if r["class_code"]:
            yearly[(r["product_code"], r["class_code"])][r["year_rank"]] = r["return_pct"]

    bad, total = [], 0
    hit_by_product = collections.Counter()
    for r in _rows(conn, "SELECT * FROM class_returns WHERE row_kind='class_return'"):
        y = yearly.get((r["product_code"], r["class_code"]))
        if not y:
            continue
        for col, rank in (("return_2y", 2), ("return_3y", 3), ("return_5y", 5)):
            v, yv = r[col], y.get(rank)
            if v is None or yv is None:
                continue
            total += 1
            if abs(v - yv) < EPS:
                hit_by_product[r["product_code"]] += 1
                bad.append(f"{r['product_code']} {r['class_code']} {col}={v} "
                           f"= 연도별 {rank}년차")
    rep.add("연평균 수익률 칸에 연도별 값이 들어감", len(bad), total, bad,
            f"의심 상품: {[p for p, n in hit_by_product.items() if n >= 2]}")


def check_retail_vs_eligibility(conn, rep):
    """이름표로 매긴 "일반 가입 가능"이 가입자격 원문과 어긋나나."""
    elig = {(r["product_code"], r["class_code"]): r["eligibility"]
            for r in _rows(conn, "SELECT * FROM class_charges "
                                 "WHERE eligibility IS NOT NULL")}
    words = ("기관", "고액", "랩", "임직원", "협회", "모집합투자기구", "자집합투자기구")
    # "기관"은 "금융기관"의 일부로도 나오고, 제한 클래스를 "제외"한다는
    # 설명에도 나온다. 그대로 세면 오탐이 난다.
    #
    #   AG  : "금융기관으로부터 별도의 투자권유 없이" -> 제한이 아니다
    #   S-P : "다른 클래스[가입 자격(기관 및 고액거래자 등)에 제한이 있는
    #          클래스 제외] 보다 판매보수가 낮고" -> 남 얘기다
    #
    # 앞에 붙는 말과 "제외"가 함께 나오는 구절을 걸러 낸다.
    COMPOUND = ("금융", "판매", "운용", "신탁")
    bad, total = [], 0
    for r in _rows(conn, "SELECT * FROM class_meaning"):
        e = elig.get((r["product_code"], r["class_code"]))
        if not e:
            continue
        total += 1
        # 공백을 다 지우고 본다. 원문이 낱말 한가운데서 줄바꿈되는
        # 문서가 있어("금 융기관") 공백을 남겨 두면 "금융기관"의 "기관"을
        # 제한으로 잘못 세게 된다.
        flat = re.sub(r"\s+", "", e)
        restricted_in_text = False
        for w in words:
            for m in re.finditer(re.escape(w), flat):
                before = flat[max(0, m.start() - 2): m.start()]
                window = flat[max(0, m.start() - 60): m.end() + 60]
                if any(before.endswith(cx) for cx in COMPOUND):
                    continue
                if "제외" in window:
                    continue
                restricted_in_text = True
                break
            if restricted_in_text:
                break
        if r["retail"] and restricted_in_text:
            bad.append(f"{r['product_code']} {r['class_code']}: "
                       f"이름표는 '{r['description']}'인데 가입자격은 "
                       f"'{e[:40]}...'")
    rep.add("일반 가입 가능으로 봤는데 가입자격에 제한이 적힘", len(bad), total, bad,
            "제한 클래스를 답변에 내보내게 되는 오류다")


def _norm_code(code):
    """붙임표·대소문자·괄호만 지운 열쇠. 이건 "같은 클래스다"가 아니라
    "따져 볼 후보다"라는 뜻일 뿐이다 - 같은지는 이름표가 정한다."""
    return re.sub(r"\(.*?\)", "", code).replace("-", "").upper()


def check_lookalike_codes(conn, rep):
    """붙임표만 다른데 뜻은 다른 클래스를 찾는다.

    표기를 뭉개서 클래스를 합치고 싶어질 때가 있는데(수익률표는 "Ae",
    보수표는 "A-e"), 그렇게 하면 안 되는 경우가 실제로 있다.

        KR5114420027  C-P          수수료미징구-오프라인-개인연금
                      Cp(퇴직연금)  수수료미징구-오프라인-퇴직연금

    한 번 이걸 뭉갰다가 퇴직연금 행이 개인연금 행을 덮어써서 개인연금
    클래스가 통째로 사라진 적이 있다. 여기 잡히는 짝은 절대 합치면
    안 되는 짝이라, 목록을 남겨 두고 지켜본다."""
    groups = collections.defaultdict(list)
    for pc, cc, ft, ch, at, attrs in conn.execute(
            "SELECT product_code, class_code, fee_type, channel, "
            "account_type, attributes FROM class_meaning"):
        groups[(pc, _norm_code(cc))].append((cc, (ft, ch, at, attrs)))

    trap = []
    for (pc, _key), rows in sorted(groups.items()):
        if len(rows) < 2 or len({m for _c, m in rows}) < 2:
            continue
        trap.append(f"{pc}: " + " / ".join(
            f"{c}={m[2] or m[1]}" for c, m in sorted(rows)))
    rep.add("표기를 뭉개면 안 되는 클래스 짝", len(trap), 0, trap,
            "표기를 뭉개면 개인연금과 퇴직연금이 한 행으로 합쳐진다 - "
            "합치기 전에 이름표로 같은지 확인해야 한다", info=True)


def check_class_code_consistency(conn, rep):
    """수익률·수수료 표에는 있는데 보수표에는 없는 클래스를 찾는다.

    표기가 갈린 것(A-e / Ae)이면 맞춰야 하고, 아예 없는 것이면 보수를
    못 읽은 것이다. 뒤엣것이 특히 아픈데, 우리가 수수료를 말할 때 그
    클래스를 통째로 빼고 말하게 되기 때문이다.

    둘을 가르는 건 표기가 아니라 이름표다 - 붙임표만 다른데 뜻이 다른
    짝이 실재하기 때문이다(check_lookalike_codes 참고)."""
    fees = collections.defaultdict(set)
    for pc, cc in conn.execute("SELECT product_code, class_code FROM class_fees"):
        fees[pc].add(cc)
    meaning = {}
    for pc, cc, retail, acct, ft, ch, attrs in conn.execute(
            "SELECT product_code, class_code, retail, account_type, "
            "fee_type, channel, attributes FROM class_meaning"):
        meaning[(pc, cc)] = {"retail": retail, "acct": acct,
                             "label": (ft, ch, acct, attrs)}

    elsewhere = collections.defaultdict(set)
    for table in ("class_returns", "class_charges", "yearly_returns"):
        for pc, cc in conn.execute(
                f"SELECT product_code, class_code FROM {table} "
                "WHERE class_code IS NOT NULL"):
            if pc in fees:
                elsewhere[pc].add(cc)

    spelling, missing = [], []
    by_kind = collections.Counter()
    for pc in sorted(fees):
        by_key = collections.defaultdict(set)
        for c in fees[pc]:
            by_key[_norm_code(c)].add(c)
        for cc in sorted(elsewhere[pc] - fees[pc]):
            twins = by_key.get(_norm_code(cc), set())
            mine = meaning.get((pc, cc))
            # 보수표에 같은 열쇠의 코드가 하나뿐이고, 이름표까지 같을 때만
            # "표기가 갈린 것"으로 본다.
            if len(twins) == 1:
                other = meaning.get((pc, next(iter(twins))))
                if mine and other and mine["label"] == other["label"]:
                    spelling.append(
                        f"{pc} {cc} (보수표에는 {next(iter(twins))}로 있음)")
                    continue
            kind = ("이름표 없음" if mine is None
                    else "일반 가입 불가" if not mine["retail"]
                    else mine["acct"] or "일반")
            by_kind[kind] += 1
            missing.append(f"{pc} {cc} [{kind}]")

    rep.add("이름표가 같은데 표기가 갈린 클래스", len(spelling), 0, spelling,
            "A-e/Ae처럼 갈리면 클래스별 답이 둘로 쪼개진다")
    rep.add("보수표에 없는 클래스", len(missing), 0,
            missing + [f"  -> {k} {v}건" for k, v in by_kind.most_common()],
            "요약정보 보수표에만 있는 클래스를 읽고 있어서, 뒤쪽 상세 "
            "보수표에만 실린 클래스는 수수료를 말할 때 통째로 빠진다")


def check_source_conflicts(conn, rep):
    """같은 문서의 두 표가 같은 값을 다르게 적은 곳을 센다.

    간이투자설명서는 같은 값을 앞쪽 요약표와 뒤쪽 상세표에 두 번 싣는다.
    총보수·판매보수는 어긋난 적이 없어서 "이 표가 이 상품의 보수표인가"를
    가리는 데 쓰지만, 총보수·비용은 두 곳이 다른 문서가 있다.

        KR5110501016 종류A
        3쪽(요약표)  0.31 = 총보수 + 0.01 (전 클래스 일괄)
        27쪽(상세표) 0.30 = 총보수 + 그 클래스 기타비용("-")

    어느 쪽이 맞다고 판정할 근거가 없어서 둘 다 담는다. 여기서 세는 건
    "고쳐야 할 오류"가 아니라 "문서가 두 곳에서 다르게 말하는 자리"다 -
    답변에 그 값을 쓸 때 한 표 안에서만 뽑고 있는지 지켜보기 위한 것.

    다만 총보수와 판매보수가 갈리면 그건 다른 얘기다. 이 둘이 갈리면
    상세표를 잘못 읽은 것이므로 실패로 센다."""
    rows = _rows(conn, "SELECT * FROM class_fee_sources")
    by_key = collections.defaultdict(dict)
    for r in rows:
        by_key[(r["product_code"], r["class_code"], r["field"])][r["source"]] = r

    soft, hard = [], []
    for (pc, cc, field), bysrc in sorted(by_key.items()):
        if len(bysrc) < 2:
            continue
        vals = {s: (r["value"] or "").strip() for s, r in bysrc.items()}
        # 자릿수 표기만 다른 건 다른 값이 아니다("0.05"와 "0.050",
        # "0.2350"과 "0.235"). 숫자로 견준다. 둘 다 "-"면 "문서가 없다고
        # 적은 것"이라 이것도 같은 값이다.
        nums = []
        for v in vals.values():
            try:
                nums.append(float(v))
            except ValueError:
                nums.append(v)
        if all(isinstance(n, float) for n in nums):
            if max(nums) - min(nums) <= EPS:
                continue
        elif len(set(nums)) < 2:
            continue
        where = " / ".join(f"{s} {vals[s]}(p{bysrc[s]['page']})"
                           for s in sorted(bysrc))
        line = f"{pc} {cc} {field}: {where}"
        (hard if field in ("total_fee", "distribution_fee") else soft).append(line)

    rep.add("총보수·판매보수를 두 표가 다르게 적음", len(hard), len(by_key), hard,
            "이 둘은 어긋난 적이 없는 값이다 - 갈리면 상세표를 잘못 읽은 것")
    rep.add("두 표가 다르게 적은 값(총보수·비용 등)", len(soft), len(by_key), soft,
            "문서가 두 곳에서 다르게 말하는 자리다. 고칠 게 아니라 "
            "답변에 쓸 때 한 표 안에서만 뽑으면 된다", info=True)


def check_asset_mix(conn, rep):
    """자산구성 비율이 100%가 되나.

    자산별 비중은 서로 더하면 100이어야 한다. 안 맞으면 열을 잘못
    짚었거나(금액 칸을 비율로 읽었거나) 자산 하나를 통째로 빠뜨린
    것이다. 이 표는 답변에 "주식 97.6%"처럼 바로 나가는 값이라
    한 칸만 어긋나도 그대로 틀린 답이 된다."""
    tot = collections.defaultdict(float)
    for pc, pct in conn.execute(
            "SELECT product_code, pct FROM asset_mix WHERE pct IS NOT NULL"):
        tot[pc] += pct
    bad = [f"{pc}: 비율 합 {v:.2f}%" for pc, v in sorted(tot.items())
           if abs(v - 100) > 1.0]
    rep.add("자산구성 비율 합이 100%가 아님", len(bad), len(tot), bad,
            "열을 잘못 짚었거나 자산 하나를 빠뜨린 것이다")

    # 기준일이 없으면 "언제 기준 비중인지" 없이 숫자만 내보내게 된다.
    n = conn.execute("SELECT COUNT(DISTINCT product_code) FROM asset_mix").fetchone()[0]
    miss = [r[0] for r in conn.execute(
        "SELECT DISTINCT product_code FROM asset_mix WHERE as_of IS NULL")]
    rep.add("자산구성에 기준일이 없음", len(miss), n, miss,
            "비중은 시점에 따라 바뀌는 값이라 기준일 없이 말하면 안 된다",
            info=True)


def check_as_of(conn, rep):
    bad = []
    rows = _rows(conn, "SELECT DISTINCT product_code, as_of FROM class_fees")
    for r in rows:
        a = r["as_of"]
        if a is None:
            bad.append(f"{r['product_code']}: 작성기준일 없음")
        elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a) or not (AS_OF_MIN <= a <= AS_OF_MAX):
            bad.append(f"{r['product_code']}: 작성기준일 {a}")
    rep.add("작성기준일이 없거나 범위 밖", len(bad), len(rows), bad)


def check_trade_rules(conn, rep):
    """기준가 규칙에 도표 잔해나 빠진 조건이 없나."""
    junk_words = ("자금납입일", "수익증권매입일", "기준가적용일", "매입청구일)")
    bad_junk, bad_half = [], []
    rows = _rows(conn, "SELECT * FROM trade_rules")
    for r in rows:
        t = r["text"]
        # 도표 잔해는 낱말로도 오지만 글자가 한 자씩 흩어져 오기도 한다
        # ("( 자 1 7 금 시 D 납 이 입 전 )").
        toks = t.split()
        runs, run = 0, 0
        for tk in toks:
            run = run + 1 if len(tk) == 1 else 0
            runs = max(runs, run)
        if any(w in t.replace(" ", "") for w in junk_words) or runs > 5:
            bad_junk.append(f"{r['product_code']} {r['kind']}: {t[:60]}")
        # 온전한 규칙에는 이전/경과 후 두 갈래가 다 있고, 뒤엣 갈래도
        # 며칠 뒤인지까지 적혀 있어야 한다. 여는 말만 있고 끊긴 조각
        # ("... 오후 5시 경과 후 자금을 납입한 경우")은 답이 못 된다.
        m = re.search(r"경과\s*후|이\s*후|초과", t)
        if not (m and re.search(r"영업일|D\s*\+\s*\d", t[m.end():])):
            bad_half.append(f"{r['product_code']} {r['kind']}: {t[:60]}")
    rep.add("기준가 규칙에 도표 글자가 섞임", len(bad_junk), len(rows), bad_junk)
    rep.add("기준가 규칙에 '경과 후' 갈래가 없음", len(bad_half), len(rows), bad_half,
            "몇 시 이후에 신청하면 어떻게 되는지가 빠진 답이 된다")


def check_yearly_periods(conn, rep):
    """연도별 표인데 기간이 다 같은 날 끝나면 연평균 표를 읽은 것이다."""
    per = collections.defaultdict(set)
    for r in _rows(conn, "SELECT * FROM yearly_returns WHERE period IS NOT NULL"):
        per[r["product_code"]].add(r["period"])
    bad = []
    for pc, ps in per.items():
        ends = {p.split("~")[1] for p in ps}
        if len(ps) > 1 and len(ends) == 1:
            bad.append(f"{pc}: 기간 {len(ps)}개가 모두 {ends.pop()}에 끝남")
    rep.add("연도별 수익률인데 기간이 모두 같은 날 끝남", len(bad), len(per), bad,
            "연평균 표를 연도별로 잘못 읽은 것이다")


def check_returns_range(conn, rep):
    """수익률이 상식 밖인가."""
    bad, total = [], 0
    for table, cols in (("class_returns",
                         ("return_1y", "return_2y", "return_3y", "return_5y",
                          "return_since_inception")),
                        ("yearly_returns", ("return_pct",))):
        for r in _rows(conn, f"SELECT * FROM {table}"):
            for c in cols:
                v = r[c]
                if v is None:
                    continue
                total += 1
                if not -100.0 <= v <= 500.0:
                    bad.append(f"{table} {r['product_code']} "
                               f"{r.get('class_code')} {c}={v}")
    rep.add("수익률이 -100% ~ 500% 밖", len(bad), total, bad)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--show", type=int, default=5, help="사례를 몇 개까지 볼지")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rep = Report()
    for fn in (check_fee_internal, check_avg_vs_yearly, check_retail_vs_eligibility,
               check_lookalike_codes, check_class_code_consistency,
               check_source_conflicts, check_asset_mix,
               check_as_of, check_trade_rules,
               check_yearly_periods, check_returns_range):
        fn(conn, rep)
    conn.close()

    worst = rep.show(args.show)
    print()
    print("모든 검사 통과" if worst == 0 else "위 항목을 확인할 것")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
