"""분류 로직 테스트. 문구는 2026-11-19 고향사랑e음 실제 공지에서 가져왔다."""
import unittest
from datetime import datetime

from gohyang_npay import (build_diff, cards_match, classify, extract_flags, extract_quota, pick_gifts,
                          render_html)


class ClassifyTest(unittest.TestCase):
    def cat(self, name, text):
        return classify(name, text)["category"]

    def test_firstcome_in_body(self):
        self.assertEqual(
            self.cat(
                "【경남 양산시】♥11월의 특별혜택, 네이버페이 2만원 이벤트♥",
                "나. 이벤트내용 : 이벤트 기간내 10만원 이상 기부자에게 선착순 500명 네이버페이 2만원 쿠폰 증정",
            ),
            "firstcome",
        )

    def test_firstcome_amount_before_npay(self):
        self.assertEqual(
            self.cat(
                "울산 중구 고향사랑기부제 홍보 이벤트",
                "○ 경 품: 선착순 500명 ▶ 2만원 상당의 네이버페이 모발일쿠폰 발송 ○ 당첨확인: 이벤트 종료 후",
            ),
            "firstcome",
        )

    def test_firstcome_n_pay_spelling(self):
        self.assertEqual(
            self.cat("♡ N페이 2만증정(선착순1,000명)", "'N 페이 2만원' (선착순1,000명)"),
            "firstcome",
        )

    def test_all_donors(self):
        self.assertEqual(
            self.cat(
                "[영암군] 🎁영민씨가 쏜다(네이버페이 2만원 전원증정)🎁",
                "이벤트혜택 : 네이버 페이 2만원(전원) 12월 중 문자발송",
            ),
            "all",
        )

    def test_all_donors_ignores_separate_draw_prize(self):
        self.assertEqual(
            self.cat(
                "💟네이버페이 2만원 전원증정+기아타이거즈 유니폼 경품 이벤트! 💟",
                "10만원이상 기부 시 네이버페이 2만원 전원 지급 🤩 +30명을 추첨하여 유니폼 증정 "
                "★ 3 만원 상당 답례품 + 네이버페이 2 만원 전원 증정 ★",
            ),
            "all",
        )

    def test_draw(self):
        self.assertEqual(
            self.cat(
                "[충북 진천군] 11월 이벤트 🍀네이버페이 2만원🍀",
                "✔️ 기 부 혜 택 : 네이버페이 2 만원권 증정 (200 명 추첨 )",
            ),
            "draw",
        )

    def test_typo_spelling_of_naverpay(self):
        self.assertEqual(
            self.cat("화순군 이벤트", "네이퍼페이 2만원! (참여자 전원)"),
            "all",
        )

    def test_other_amount(self):
        self.assertEqual(
            self.cat(
                "[네이버페이 1만원 증정] 구리시 고향사랑기부 이벤트",
                "○ 제공혜택 : 네이버페이 모바일상품권 (1 만원권 ) ○ 선정인원 : 선착순 150 명",
            ),
            "other_amount",
        )

    def test_no_naverpay(self):
        self.assertEqual(
            self.cat("[경남 창녕] 구글플레이 2만원권", "구글플레이 모바일 교환권 2만 원권: 선착순 500명"),
            "none",
        )

    def test_naver_form_is_not_naverpay(self):
        self.assertEqual(
            self.cat("탐조투어 무료 제공", "신청방법 : 온라인 네이버폼 작성 탐조투어 2 인 (2 만원 )"),
            "none",
        )

    def test_closed_notice(self):
        self.assertEqual(
            self.cat(
                "[밀양시] 밀양시 11월 이벤트 조기 종료 안내",
                "11월 네이버포인트 2만원권 증정이벤트가 예 산소진으로 조기 종료 되었음을 안내드립니다.",
            ),
            "closed",
        )

    def test_budget_clause_is_not_closed(self):
        self.assertEqual(
            self.cat("이벤트", "선착순 500명 네이버페이 2만원 ※ 예산 소진 시 조기 종료 될 수 있음"),
            "firstcome",
        )


class ReviewTest(unittest.TestCase):
    def result(self, name, text):
        return classify(name, text)

    def test_image_only_needs_review(self):
        r = self.result("2026년 하동군 고향사랑기부제 11월 한정 「네이버페이 2만원」 이벤트", "")
        self.assertEqual((r["category"], r["reason"]), ("review", "image_only"))

    def test_image_only_without_hint_still_needs_review(self):
        r = self.result("울산 북구 고향사랑기부제 연말정산 맞이 이벤트", " ")
        self.assertEqual((r["category"], r["reason"]), ("review", "image_only"))

    def test_mixed_modes_need_review(self):
        r = self.result(
            "부산 중구 고향사랑기부제 연말 이벤트",
            "▷ [추 첨] 총 20명 : 네이버페이 쿠폰 3만원권 ▷ [선착순] 총 220명 : 네이버페이 쿠폰 2만원권 "
            "※ 추첨 당첨자는 기간내 전체 기부자 중에서 추첨",
        )
        self.assertEqual((r["category"], r["reason"]), ("review", "ambiguous"))

    def test_special_order_needs_review(self):
        r = self.result(
            "💸네이버페이 1만원💸달성군 이벤트",
            "모바일상품권(네이버페이) 1만원권 👉 선착순 191 명 · 모바일상품권(네이버페이) 2만원권 추가 증정 "
            "👉 특정 순번(1, 50, 100번째) 기부자",
        )
        self.assertEqual((r["category"], r["reason"]), ("review", "ambiguous"))

    def test_no_mode_needs_review(self):
        r = self.result(
            "♥무주군 최대 15만원의 혜택 네이베퍼이 2만 원 이벤트♥",
            "○ 인원: 500명 ○ 경품: 네이버페이 2만원 ○ 발표: 12월 중",
        )
        self.assertEqual((r["category"], r["reason"]), ("review", "no_mode"))

    def test_no_amount_needs_review(self):
        r = self.result(
            "진도愛 재기부하면 혜택이 두배! 세배!",
            "답례품 제공(기부액 30%) 네이버 페이 포인트 제공 4️⃣ 당첨자발표 : 순차 발송",
        )
        self.assertEqual((r["category"], r["reason"]), ("review", "no_amount"))


class ExtractTest(unittest.TestCase):
    def test_quota(self):
        self.assertEqual(extract_quota("N페이 2만원 (선착순1,000명)"), 1000)
        self.assertEqual(extract_quota("[선착순] 총 220명 : 네이버페이"), 220)
        self.assertIsNone(extract_quota("전원 증정"))

    def test_flags(self):
        flags = extract_flags(
            "고향사랑e음 또는 위기브 접속 → 10만원 이상 기부하고 답례품 주문까지 완료하면 자동 응모! "
            "sms 수신 동의 필수 ※ 예산 소진 시 까지 ※ 1인 1회 한정 특정사업에 기부"
        )
        self.assertEqual(
            flags,
            ["답례품 주문 필요", "SMS 수신 동의 필수", "1인 1회", "위기브 가능", "지정(특정)사업 기부", "예산 소진 시 조기 종료"],
        )


class GiftTest(unittest.TestCase):
    def item(self, name, price, sold_out=None, code="48330"):
        return {"itemName": name, "salePrice": price, "itemSoldOutFlag": sold_out,
                "itemUserCode": "G1", "locgovCode": code, "locgovNm": "경상남도 양산시"}

    def test_picks_affordable_items_most_expensive_first(self):
        items = [
            self.item("노트 &quot;한마디&quot;", 7000),
            self.item("한우 세트", 50000),
            self.item("식사할인권", 30000),
            self.item("품절 쌀", 30000, sold_out="Y"),
            self.item("오란다", 10000),
        ]
        picked = pick_gifts(items, max_points=30000, limit=2)
        self.assertEqual([(g["name"], g["points"]) for g in picked], [("식사할인권", 30000), ("오란다", 10000)])
        self.assertEqual(pick_gifts(items, 30000, 5)[-1]["name"], '노트 "한마디"')

    def test_cards_match_region(self):
        items = [self.item("a", 1000), self.item("b", 1000)]
        self.assertTrue(cards_match(items, "48330"))
        self.assertFalse(cards_match(items + [self.item("c", 1000, code="48000")], "48330"))
        self.assertFalse(cards_match([], "48330"))


class HtmlTest(unittest.TestCase):
    def rows(self):
        return {
            "1": {"id": "1", "name": "행사 <script>alert(1)</script>", "host": "양산시", "region": "경상남도 양산시",
                  "start": "20261104", "end": "20261130", "hash": "a", "quota": 500, "flags": ["1인 1회"],
                  "note": "", "category": "firstcome", "reason": "", "locgov": "48330", "mall_ok": True,
                  "gifts": [{"name": "길된장 <2kg>", "points": 30000, "code": "G1"}]},
            "2": {"id": "2", "name": "포스터만", "host": "하동군", "region": "경상남도 하동군", "start": "20261117",
                  "end": "20261130", "hash": "b", "quota": None, "flags": [], "note": "", "category": "review",
                  "reason": "image_only", "locgov": "48850", "posters": ["https://ilovegohyang.go.kr/p.png"]},
        }

    def test_page_has_links_and_escapes_text(self):
        page = render_html(self.rows(), None, datetime(2026, 11, 19, 14, 50))
        self.assertIn("2026-11-19 14:50 KST", page)
        self.assertIn("전일 비교 불가", page)
        self.assertIn("featured/eventDetail.html?pages=1", page)
        self.assertIn("goods/searchGoods-main.html?type=L&amp;locgov=48330", page)
        self.assertIn("items/details-main.html?code=G1", page)
        self.assertIn("길된장 &lt;2kg&gt;", page)
        self.assertIn("선착순 500명", page)
        self.assertIn("https://ilovegohyang.go.kr/p.png", page)
        self.assertNotIn("<script>alert", page)

    def test_empty_main_table_says_none(self):
        page = render_html({}, None, datetime(2026, 11, 19, 9, 0))
        self.assertIn("오늘 확인된 선착순 2만 원 행사 없음", page)


class DiffTest(unittest.TestCase):
    def row(self, cat="firstcome", quota=500, end="20261130", h="a"):
        return {"category": cat, "quota": quota, "end": end, "hash": h}

    def test_first_run_has_no_diff(self):
        self.assertIsNone(build_diff(None, {"1": self.row()}))

    def test_new_changed_closed(self):
        prev = {"1": self.row(), "2": self.row(), "3": self.row()}
        today = {
            "1": self.row(),
            "2": self.row(quota=300, h="b"),
            "3": self.row(cat="closed", h="c"),
            "4": self.row(),
            "5": self.row(cat="all"),
        }
        self.assertEqual(build_diff(prev, today), {"new": ["4"], "changed": ["2"], "closed": ["3"], "missing": []})

    def test_missing_is_not_closed(self):
        diff = build_diff({"1": self.row()}, {})
        self.assertEqual(diff, {"new": [], "changed": [], "closed": [], "missing": ["1"]})


if __name__ == "__main__":
    unittest.main()
