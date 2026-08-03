import unittest
import json
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

import monitor


BOARD_URL = "https://www.seoultech.ac.kr/service/info/notice/"


class NoticeListParserTests(unittest.TestCase):
    def parse_notices(self, html):
        soup = BeautifulSoup(html, "html.parser")
        with patch.object(monitor, "request_soup", return_value=soup):
            return monitor.extract_recent_notice_links(BOARD_URL, date(2026, 1, 1))

    def test_uses_date_cell_instead_of_date_inside_title(self):
        notices = self.parse_notices(
            """
            <table><tr>
              <td>1</td>
              <td class="title"><a href="?do=commonview&amp;bidx=101&amp;bnum=4691">
                2026-12-31 마감 인턴 모집
              </a></td>
              <td>취업진로본부</td>
              <td class="date">2026-08-01</td>
            </tr></table>
            """
        )

        self.assertEqual(notices[0]["date"], "2026-08-01")

    def test_prefers_visible_link_in_title_cell_over_other_links(self):
        notices = self.parse_notices(
            """
            <table><tr>
              <td class="title">
                <a href="?do=commonview&amp;bidx=102&amp;bnum=4691">AI 특강 모집</a>
              </td>
              <td>
                <a href="?do=commonview&amp;bidx=102&amp;bnum=4691" title="새 창 열림">더보기 +</a>
              </td>
              <td class="date">2026-08-02</td>
            </tr></table>
            """
        )

        self.assertEqual(notices[0]["title"], "AI 특강 모집")

    def test_skips_row_without_a_dedicated_posted_date(self):
        notices = self.parse_notices(
            """
            <table><tr>
              <td class="title">
                <a href="?do=commonview&amp;bidx=103&amp;bnum=4691">인턴 모집 (2026-08-30 마감)</a>
              </td>
              <td>취업진로본부</td>
            </tr></table>
            """
        )

        self.assertEqual(notices, [])


class DisplayTitleTests(unittest.TestCase):
    def test_removes_promotion_prefix_and_trailing_deadline(self):
        title = "(홍보) 2026년 하반기 서울인재대학장학금 선발 안내(~8/10(월까지)"

        self.assertEqual(
            monitor.clean_display_title(title),
            "2026년 하반기 서울인재대학장학금 선발 안내",
        )

    def test_supports_other_promotion_brackets(self):
        self.assertEqual(
            monitor.clean_display_title("[외부홍보] AI 교육 참가자 모집 [~8.31까지]"),
            "AI 교육 참가자 모집",
        )

    def test_keeps_meaningful_parentheses(self):
        title = "AI 로봇 교육(온라인) 참가자 모집"

        self.assertEqual(monitor.clean_display_title(title), title)


class ExcludedNoticeTests(unittest.TestCase):
    def test_excludes_unwanted_notice_types(self):
        titles = [
            "서울과학기술대학교 학칙 일부개정 공고",
            "2026학년도 교수모집 안내",
            "전임교원 초빙 공고",
            "2026년 학생예비군 훈련 안내",
            "제2생활관 생활관생 모집",
            "학습 튜터 모집 안내",
            "제15기 학생홍보대사 모집",
            "병무청 청춘예찬 기자단 모집",
            "육군 현역병 입영 안내",
        ]

        for title in titles:
            with self.subTest(title=title):
                self.assertTrue(monitor.is_excluded_notice(title))

    def test_does_not_exclude_similar_useful_notice(self):
        self.assertFalse(
            monitor.is_excluded_notice("교수학습법 AI 특강 참가자 모집")
        )

    def test_excluded_scholarship_board_notice_is_seen_without_notification(self):
        notice = {
            "id": "bidx:999",
            "title": "병무청 현역병 모집 안내",
            "url": "https://example.com/notice/999",
            "date": "2026-08-03",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            seen_path = Path(tmpdir) / "seen.json"
            seen_path.write_text('{"장학공지": []}', encoding="utf-8")

            with (
                patch.object(monitor, "SEEN_PATH", seen_path),
                patch.object(
                    monitor,
                    "BOARDS",
                    [{"name": "장학공지", "url": "https://example.com"}],
                ),
                patch.object(
                    monitor,
                    "extract_recent_notice_links",
                    return_value=[notice],
                ),
                patch.object(monitor, "extract_detail_body") as extract_body,
                patch.object(monitor, "notify") as notify,
            ):
                monitor.main()

            saved = json.loads(seen_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["장학공지"][0]["id"], notice["id"])
            extract_body.assert_not_called()
            notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
