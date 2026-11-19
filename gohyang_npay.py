#!/usr/bin/env python3
"""고향사랑e음 지역이벤트에서 '네이버페이 2만 원 선착순' 행사를 찾아 Markdown 보고서를 만든다.

공개 API만 조회한다(로그인·기부·주문·신청 없음). 표준 라이브러리만 쓴다.

  python3 gohyang_npay.py                    # 오늘 보고서 출력 + state/ 에 저장
  python3 gohyang_npay.py --html docs/index.html   # 공유용 HTML 한 장도 함께 생성
  python3 gohyang_npay.py --gifts 5          # 지자체별 답례품 5개씩 (기본 3)
  python3 gohyang_npay.py --posters          # '확인 필요' 행사의 포스터를 posters/ 에 저장
  python3 gohyang_npay.py --review 6036 firstcome --quota 500 --note "포스터로 확인"

본문만으로 지급 방식을 판정할 수 없는 행사(이미지 전용, 선착순·추첨 혼재 등)는 '확인 필요'로
나온다. 포스터나 본문을 읽고 --review 로 판정을 기록하면 공지 내용이 바뀌기 전까지 재사용된다.
판정값: firstcome(선착순) all(전원) conditional(조건부) draw(추첨) other(대상 아님) closed(종료)
"""
import argparse
import hashlib
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://ilovegohyang.go.kr"
WEB = "https://www.ilovegohyang.go.kr"
HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
OVERRIDES = HERE / "overrides.json"
KST = ZoneInfo("Asia/Seoul")

REVIEW_CATEGORIES = ("firstcome", "all", "conditional", "draw", "other", "closed")

# ---------------------------------------------------------------- 분류 (순수 함수)

NPAY = re.compile(r"네이[버퍼베](페이|퍼이|포인트|pay)|N(페이|pay)", re.I)
TWENTY = re.compile(r"(?<!\d)2만(원|포인트|증정|권)|(?<!\d)20,000(원|포인트)")
AMOUNT = re.compile(r"(?<!\d)[1-9]만(원|포인트|권)|(?<!\d)\d{1,2},000(원|포인트)")
CLOSED = re.compile(r"(종료|마감)(되었|됐|안내)")
MODES = {
    "firstcome": re.compile(r"선착순"),
    "all": re.compile(r"전원|모두에게|100%"),
    "draw": re.compile(r"추첨|랜덤"),
    "special": re.compile(r"순번|배수"),
}
NEAR_AFTER, NEAR_BEFORE, MODE_WINDOW = 15, 8, 25


def _squash(s):
    return re.sub(r"\s+", "", s or "")


def _tied(npay_spans, amount_spans):
    """네이버페이 언급 바로 옆(뒤 15자·앞 8자 이내)에 붙은 금액 언급만 고른다."""
    out = []
    for a_start, a_end in amount_spans:
        for n_start, n_end in npay_spans:
            if 0 <= a_start - n_end <= NEAR_AFTER or 0 <= n_start - a_end <= NEAR_BEFORE:
                out.append((min(a_start, n_start), max(a_end, n_end)))
                break
    return out


def _votes(s):
    """문자열 하나에서 '네이버페이 2만 원' 언급마다 주변 지급 방식을 모은다."""
    npay = [m.span() for m in NPAY.finditer(s)]
    twenty = _tied(npay, [m.span() for m in TWENTY.finditer(s)])
    other = _tied(npay, [m.span() for m in AMOUNT.finditer(s) if not TWENTY.match(s, m.start())])
    votes, mixed = [], False
    for start, end in twenty:
        window = s[max(0, start - MODE_WINDOW): end + MODE_WINDOW]
        kinds = {k for k, rx in MODES.items() if rx.search(window)}
        if len(kinds) == 1:
            votes.append(kinds.pop())
        elif kinds:
            mixed = True
    return {"npay": bool(npay), "twenty": bool(twenty), "other": bool(other), "votes": votes, "mixed": mixed}


def classify(name, text):
    """행사 제목과 본문(태그 제거된 텍스트)으로 네이버페이 2만 원의 지급 방식을 판정한다."""
    parts = [_votes(_squash(name)), _votes(_squash(text))]
    body = _squash(text)

    def result(category, reason=""):
        return {"category": category, "reason": reason}

    if len(body) < 5:
        return result("review", "image_only")
    if not any(p["npay"] for p in parts):
        return result("none")
    if CLOSED.search(_squash(name)) or CLOSED.search(body):
        return result("closed")
    if not any(p["twenty"] for p in parts):
        return result("other_amount") if any(p["other"] for p in parts) else result("review", "no_amount")
    kinds = {v for p in parts for v in p["votes"]}
    if len(kinds) == 1 and "special" not in kinds:
        return result(kinds.pop())
    if kinds or any(p["mixed"] for p in parts):
        return result("review", "ambiguous")
    return result("review", "no_mode")


def extract_quota(text):
    m = re.search(r"선착순\]?\s*(?:총\s*)?([\d,]+)\s*명", text or "")
    return int(m.group(1).replace(",", "")) if m else None


FLAGS = [
    ("답례품 주문 필요", re.compile(r"답례품(주문|신청)?(까지)?(주문|신청)?(을|를)?완료|답례품포인트를전액사용")),
    ("SMS 수신 동의 필수", re.compile(r"(sms|문자)수신(에)?동의", re.I)),
    ("1인 1회", re.compile(r"1인1회")),
    ("위기브 가능", re.compile(r"위기브")),
    ("지정(특정)사업 기부", re.compile(r"(지정|특정)\(?(지정|특정)?\)?(기부)?사업")),
    ("예산 소진 시 조기 종료", re.compile(r"예산소진")),
]


def extract_flags(text):
    s = _squash(text)
    return [label for label, rx in FLAGS if rx.search(s)]


def pick_gifts(items, max_points, limit):
    """기부 포인트로 받을 수 있는 답례품을 비싼 순으로 고른다(품절 제외)."""
    ok = [i for i in items if (i.get("salePrice") or 0) <= max_points and i.get("itemSoldOutFlag") != "Y"]
    ok.sort(key=lambda i: -(i.get("salePrice") or 0))
    return [
        {"name": html.unescape(i.get("itemName") or "").strip(), "points": i.get("salePrice"),
         "code": i.get("itemUserCode")}
        for i in ok[:limit]
    ]


def cards_match(items, locgov):
    return bool(items) and all(str(i.get("locgovCode")) == str(locgov) for i in items)


def build_diff(prev, today):
    """전일 대비 본표(선착순) 변화. prev 가 없으면 None."""
    if prev is None:
        return None
    was = {k for k, v in prev.items() if v["category"] == "firstcome"}
    now = {k for k, v in today.items() if v["category"] == "firstcome"}

    def changed(k):
        return any(prev[k].get(f) != today[k].get(f) for f in ("quota", "end", "hash"))

    return {
        "new": sorted(now - was),
        "changed": sorted(k for k in now & was if changed(k)),
        "closed": sorted(k for k in was if k in today and today[k]["category"] == "closed"),
        "missing": sorted(k for k in was if k not in today),
    }


# ---------------------------------------------------------------- 조회

def get_json(path, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": WEB + "/"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def plain(content):
    s = html.unescape(html.unescape(content or ""))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def fetch_ongoing():
    events, page = [], 1
    while True:
        d = get_json("/api/event", {"page": page, "itemsPerPage": 200, "ingCond": "Y"})
        events += d.get("content") or []
        if page >= (d.get("totalPages") or 1):
            return events
        page += 1


def fetch_detail(event_id):
    d = get_json(f"/api/event/{event_id}", {"page": 1, "itemsPerPage": 12, "featuredUrl": event_id})
    return d["featuredList"]["featured"]


def fetch_items(locgov):
    d = get_json("/api/item/list/list-new", {
        "page": 1, "itemsPerPage": 300, "keyword": "", "searchGbn": "L",
        "sortType": "HIGHPRICE", "govList": locgov})
    return d.get("content") or []


def poster_urls(detail):
    content = html.unescape(html.unescape(detail.get("featuredContent") or ""))
    urls = re.findall(r"<img[^>]+src=[\\\"']+([^\"'\\ >]+)", content)
    if detail.get("featuredImageSrc"):
        urls.append(detail["featuredImageSrc"])
    return [u if u.startswith("http") else API + u for u in dict.fromkeys(urls)]


def content_hash(name, text):
    return hashlib.sha256((name + "\n" + text).encode()).hexdigest()[:16]


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def collect(gift_limit, max_points):
    overrides = load_json(OVERRIDES, {})
    rows = {}
    for e in fetch_ongoing():
        eid = str(e["featuredId"])
        name, text = e.get("featuredName") or "", plain(e.get("featuredContent"))
        row = {
            "id": eid, "name": name.strip(), "host": (e.get("featuredHost") or "").strip(),
            "start": e.get("startDate"), "end": e.get("endDate"), "hash": content_hash(name, text),
            "quota": extract_quota(name + " " + text), "flags": extract_flags(text), "note": "",
            **classify(name, text),
        }
        ov = overrides.get(eid)
        if ov and ov.get("hash") == row["hash"]:
            row.update(category=ov["category"], reason="reviewed", note=ov.get("note", ""))
            if ov.get("quota"):
                row["quota"] = ov["quota"]
        elif ov:
            row.update(category="review", reason="content_changed")
        rows[eid] = row

    for row in rows.values():
        if row["category"] not in ("firstcome", "all", "conditional", "review", "closed"):
            continue
        detail = fetch_detail(row["id"])
        row["locgov"] = str(detail.get("locgovCode") or "")
        row["region"] = detail.get("locgovName") or row["host"]
        if row["category"] == "review":
            row["posters"] = poster_urls(detail)
        if row["category"] in ("firstcome", "all", "conditional") and row["locgov"]:
            items = fetch_items(row["locgov"])
            row["mall_ok"] = cards_match(items, row["locgov"])
            row["gifts"] = pick_gifts(items, max_points, gift_limit)
        time.sleep(0.1)
    return rows


# ---------------------------------------------------------------- 보고서

def d(ymd):
    return f"{int(ymd[4:6])}/{int(ymd[6:8])}" if ymd and len(ymd) == 8 else "?"


def event_link(row):
    return f"[{row['id']}]({WEB}/featured/eventDetail.html?pages={row['id']})"


def mall_link(row):
    if not row.get("locgov"):
        return "지역코드 미확인"
    label = row["locgov"] if row.get("mall_ok") else f"{row['locgov']} (상품 표시 미확인)"
    return f"[{label}]({WEB}/goods/searchGoods-main.html?type=L&locgov={row['locgov']})"


def gifts_cell(row):
    gifts = row.get("gifts") or []
    return "<br>".join(f"{g['name']} ({g['points']:,}P)" for g in gifts) or "-"


def condition_cell(row):
    quota = f"선착순 {row['quota']:,}명" if row.get("quota") else "인원 미확인"
    extra = [row["note"]] if row.get("note") else []
    return " · ".join([quota] + row["flags"] + extra)


REASONS = {
    "image_only": "본문 없음(포스터만)", "ambiguous": "선착순·추첨·전원 문구 혼재",
    "no_mode": "지급 방식 문구 없음", "no_amount": "네이버페이 금액 문구 없음",
    "content_changed": "공지 내용이 바뀜(재확인)",
}


def render(rows, diff, now):
    by = lambda cat: sorted((r for r in rows.values() if r["category"] == cat), key=lambda r: (r["end"], r["id"]))
    out = []
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if diff is None:
        out.append(f"**{stamp} | 전일 비교 불가**")
    else:
        out.append(f"**{stamp} | 신규 {len(diff['new'])}건 · 조건 변경 {len(diff['changed'])}건 · "
                   f"종료 확인 {len(diff['closed'])}건**")
        for label, key in (("신규", "new"), ("조건 변경", "changed"), ("종료 확인", "closed")):
            for eid in diff[key]:
                out.append(f"- {label}: {rows[eid].get('region', rows[eid]['host'])} {event_link(rows[eid])}")
        for eid in diff["missing"]:
            out.append(f"- 진행 목록에서 사라짐(종료로 단정하지 않음, 확인 필요): 이벤트 {eid} "
                       f"{WEB}/featured/eventDetail.html?pages={eid}")

    out.append("\n## 선착순 2만 원")
    main = by("firstcome")
    if main:
        out.append("| 지자체 | 2만 원 지급 조건·선착순 인원 | 기간 | 잔여 상태 | 이벤트 공지 | 답례품몰 바로가기 | 답례품 예시 |")
        out.append("|---|---|---|---|---|---|---|")
        for r in main:
            out.append(f"| {r.get('region', r['host'])} | {condition_cell(r)} | {d(r['start'])}~{d(r['end'])} | "
                       f"공지상 기간 내 · 잔여 미확인 | {event_link(r)} | {mall_link(r)} | {gifts_cell(r)} |")
    else:
        out.append("오늘 확인된 선착순 2만 원 행사 없음")

    for title, cat in (("2만 원 전원 지급", "all"), ("조건부 2만 원", "conditional")):
        group = by(cat)
        if group:
            out.append(f"\n## {title}")
            out.append("| 지자체 | 조건 | 기간 | 이벤트 공지 | 답례품몰 바로가기 | 답례품 예시 |")
            out.append("|---|---|---|---|---|---|")
            for r in group:
                cond = " · ".join(r["flags"] + ([r["note"]] if r["note"] else [])) or "-"
                out.append(f"| {r.get('region', r['host'])} | {cond} | {d(r['start'])}~{d(r['end'])} | "
                           f"{event_link(r)} | {mall_link(r)} | {gifts_cell(r)} |")

    closed = by("closed")
    if closed:
        out.append("\n## 종료 확인")
        out += [f"- {r.get('region', r['host'])}: {r['name']} {event_link(r)}" for r in closed]

    review = by("review")
    if review:
        out.append("\n## 확인 필요 (본문만으로 판정 불가)")
        for r in review:
            out.append(f"- {event_link(r)} {r.get('region', r['host'])} — {r['name']} "
                       f"/ 사유: {REASONS.get(r['reason'], r['reason'])}")
            out += [f"  - 포스터: {u}" for u in r.get("posters", [])]
        out.append(f"\n판정 기록: `python3 {Path(__file__).name} --review <이벤트ID> <{'|'.join(REVIEW_CATEGORIES)}>`")

    counts = {c: sum(r["category"] == c for r in rows.values()) for c in ("draw", "other", "other_amount", "none")}
    out.append(f"\n조사 범위: 고향사랑e음 진행 중 이벤트 {len(rows)}건 "
               f"(2만 원 추첨 {counts['draw']}건 · 네이버페이 다른 금액 {counts['other_amount']}건 · "
               f"대상 아님 {counts['none'] + counts['other']}건 제외). "
               "잔여 수량과 지자체 홈페이지 공지는 조회하지 않음. 답례품몰은 상품 목록 데이터로만 확인.")
    return "\n".join(out)


# ---------------------------------------------------------------- HTML

CSS = """
:root{--bg:#fff;--fg:#1c1c1e;--muted:#6b7280;--line:#e5e7eb;--accent:#03a94d;--chip:#f3f4f6}
@media(prefers-color-scheme:dark){:root{--bg:#111316;--fg:#e8eaed;--muted:#9aa0a6;--line:#2a2e33;--accent:#3ddc84;--chip:#1e2227}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic",sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 10px}
.stamp{color:var(--muted);margin:0 0 4px}.diff{margin:8px 0 0;padding-left:18px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:720px}
th,td{text-align:left;vertical-align:top;padding:9px 10px;border-bottom:1px solid var(--line)}
th{font-size:13px;color:var(--muted);font-weight:600;white-space:nowrap}
td.region{font-weight:600;white-space:nowrap}td.period{white-space:nowrap}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.chip{display:inline-block;background:var(--chip);border-radius:999px;padding:1px 9px;margin:0 4px 4px 0;font-size:13px}
.quota{background:var(--accent);color:var(--bg);font-weight:600}
ul{margin:0;padding-left:18px}li{margin:2px 0}.muted,footer{color:var(--muted);font-size:13px}footer{margin-top:36px}
"""


def render_html(rows, diff, now):
    e = html.escape
    by = lambda cat: sorted((r for r in rows.values() if r["category"] == cat), key=lambda r: (r["end"], r["id"]))

    def link(url, label):
        return f'<a href="{e(url)}" target="_blank" rel="noopener">{e(label)}</a>'

    def event(r):
        return link(f"{WEB}/featured/eventDetail.html?pages={r['id']}", "공지 보기")

    def mall(r):
        if not r.get("locgov"):
            return "지역코드 미확인"
        label = "답례품몰 열기" if r.get("mall_ok") else "답례품몰 열기 (상품 표시 미확인)"
        return link(f"{WEB}/goods/searchGoods-main.html?type=L&locgov={r['locgov']}", label)

    def gifts(r):
        items = "".join(
            f"<li>{link(f'{WEB}/items/details-main.html?code=' + str(g['code']), g['name'])} "
            f"<span class=\"muted\">{g['points']:,}P</span></li>" for g in r.get("gifts") or [])
        return f"<ul>{items}</ul>" if items else "-"

    def chips(r, with_quota):
        out = []
        if with_quota:
            out.append(f'<span class="chip quota">{e(f"선착순 {r["quota"]:,}명" if r.get("quota") else "인원 미확인")}</span>')
        out += [f'<span class="chip">{e(t)}</span>' for t in r["flags"] + ([r["note"]] if r.get("note") else [])]
        return "".join(out) or "-"

    def table(group, with_quota):
        head = "<tr><th>지자체</th><th>조건</th><th>기간</th>" + ("<th>잔여 상태</th>" if with_quota else "") + \
               "<th>이벤트 공지</th><th>답례품몰</th><th>답례품 예시</th></tr>"
        body = "".join(
            f'<tr><td class="region">{e(r.get("region", r["host"]))}</td><td>{chips(r, with_quota)}</td>'
            f'<td class="period">{d(r["start"])}~{d(r["end"])}</td>'
            + ("<td>공지상 기간 내 · 잔여 미확인</td>" if with_quota else "")
            + f"<td>{event(r)}</td><td>{mall(r)}</td><td>{gifts(r)}</td></tr>" for r in group)
        return f'<div class="scroll"><table>{head}{body}</table></div>'

    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    parts = [f"<h1>네이버페이 2만 원 · 고향사랑e음</h1>"]
    if diff is None:
        parts.append(f'<p class="stamp">{stamp} | 전일 비교 불가</p>')
    else:
        parts.append(f'<p class="stamp">{stamp} | 신규 {len(diff["new"])}건 · 조건 변경 {len(diff["changed"])}건 · '
                     f'종료 확인 {len(diff["closed"])}건</p>')
        lines = [f"<li>{label}: {e(rows[i].get('region', rows[i]['host']))} {event(rows[i])}</li>"
                 for label, key in (("신규", "new"), ("조건 변경", "changed"), ("종료 확인", "closed"))
                 for i in diff[key]]
        lines += [f"<li>진행 목록에서 사라짐(종료로 단정하지 않음): "
                  f"{link(f'{WEB}/featured/eventDetail.html?pages={i}', '이벤트 ' + i)}</li>" for i in diff["missing"]]
        if lines:
            parts.append(f'<ul class="diff">{"".join(lines)}</ul>')

    parts.append("<h2>선착순 2만 원</h2>")
    main = by("firstcome")
    parts.append(table(main, True) if main else "<p>오늘 확인된 선착순 2만 원 행사 없음</p>")
    for title, cat in (("2만 원 전원 지급", "all"), ("조건부 2만 원", "conditional")):
        if by(cat):
            parts += [f"<h2>{title}</h2>", table(by(cat), False)]
    if by("closed"):
        parts.append("<h2>종료 확인</h2><ul>" + "".join(
            f"<li>{e(r.get('region', r['host']))}: {e(r['name'])} {event(r)}</li>" for r in by("closed")) + "</ul>")
    if by("review"):
        parts.append("<h2>확인 필요 (본문만으로 판정 불가)</h2><ul>" + "".join(
            f"<li>{e(r.get('region', r['host']))} — {e(r['name'])} "
            f"<span class=\"muted\">{e(REASONS.get(r['reason'], r['reason']))}</span> {event(r)} "
            + " ".join(link(u, f"포스터 {n + 1}") for n, u in enumerate(r.get("posters", []))) + "</li>"
            for r in by("review")) + "</ul>")
    parts.append(f"<footer>고향사랑e음 진행 중 이벤트 {len(rows)}건을 조회했습니다. 잔여 수량과 지자체 홈페이지 공지는 "
                 "조회하지 않으며, 답례품몰은 상품 목록 데이터로만 확인합니다. 기부 전 이벤트 공지를 직접 확인하세요.</footer>")
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>네이버페이 2만 원 · {now.strftime('%Y-%m-%d')}</title><style>{CSS}</style></head>"
            f"<body><main>{''.join(parts)}</main></body></html>\n")


# ---------------------------------------------------------------- 실행

def cmd_review(args):
    e = next((x for x in fetch_ongoing() if str(x["featuredId"]) == args.review[0]), None)
    if e is None:
        sys.exit(f"진행 중 목록에 이벤트 {args.review[0]} 이(가) 없습니다.")
    if args.review[1] not in REVIEW_CATEGORIES:
        sys.exit(f"판정값은 {', '.join(REVIEW_CATEGORIES)} 중 하나여야 합니다.")
    overrides = load_json(OVERRIDES, {})
    name = e.get("featuredName") or ""
    entry = {"hash": content_hash(name, plain(e.get("featuredContent"))), "category": args.review[1],
             "name": name.strip(), "note": args.note or ""}
    if args.quota:
        entry["quota"] = args.quota
    overrides[args.review[0]] = entry
    save_json(OVERRIDES, overrides)
    print(f"기록: {args.review[0]} → {args.review[1]} ({name.strip()})")


def save_posters(rows):
    for r in rows.values():
        for n, url in enumerate(r.get("posters", [])):
            ext = Path(urllib.parse.urlparse(url).path).suffix or ".img"
            path = HERE / "posters" / f"{r['id']}_{n}{ext}"
            if not path.exists():
                path.parent.mkdir(exist_ok=True)
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    path.write_bytes(resp.read())


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gifts", type=int, default=3, help="지자체별 답례품 예시 개수 (기본 3)")
    p.add_argument("--max-points", type=int, default=30000, help="답례품 포인트 상한 (기본 30000 = 10만 원 기부)")
    p.add_argument("--posters", action="store_true", help="확인 필요 행사의 포스터를 posters/ 에 저장")
    p.add_argument("--review", nargs=2, metavar=("ID", "판정"), help="확인 필요 행사의 판정을 기록")
    p.add_argument("--quota", type=int, help="--review 와 함께: 선착순 인원")
    p.add_argument("--note", help="--review 와 함께: 보고서에 붙일 메모")
    p.add_argument("--no-save", action="store_true", help="state/ 에 저장하지 않음")
    p.add_argument("--html", metavar="경로", help="HTML 보고서를 이 경로에 저장 (예: docs/index.html)")
    args = p.parse_args()
    if args.review:
        return cmd_review(args)

    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    rows = collect(args.gifts, args.max_points)
    previous = [f for f in sorted(STATE_DIR.glob("20*.json")) if f.stem < today]
    diff = build_diff(load_json(previous[-1], None) if previous else None, rows)
    report = render(rows, diff, now)
    print(report)
    if args.html:
        target = Path(args.html).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_html(rows, diff, now), encoding="utf-8")
    if args.posters:
        save_posters(rows)
    if not args.no_save:
        save_json(STATE_DIR / f"{today}.json", rows)
        (STATE_DIR / f"{today}.md").write_text(report + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
