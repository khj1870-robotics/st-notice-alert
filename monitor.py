import html
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


BOARDS = [
    {
        "name": "취업공지",
        "url": "https://www.seoultech.ac.kr/service/info/job",
    },
    {
        "name": "대학공지사항",
        "url": "https://www.seoultech.ac.kr/service/info/notice",
    },
    {
        "name": "장학공지",
        "url": "https://www.seoultech.ac.kr/service/info/janghak",
    },
]

KEYWORDS = [
    "로봇",
    "인공지능",
    "AI",
    "머신러닝",
    "강화학습",
    "현장실습",
    "인턴",
    "학부 인턴",
    "연구실",
    "근로",
    "국가근로",
    "장학금",
    "모집",
]

EXCLUDE_KEYWORDS = [
    "지급완료",
    "마감",
]

SEEN_PATH = Path("seen.json")
RECENT_DAYS = 90
MAX_PAGES_PER_BOARD = 10

DATE_RE = re.compile(r"(20\d{2})[-.](\d{1,2})[-.](\d{1,2})")


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold()


def load_seen() -> dict:
    if not SEEN_PATH.exists():
        return {}
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_seen(seen: dict) -> None:
    SEEN_PATH.write_text(
        json.dumps(seen, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_known_ids(board_seen: list) -> set[str]:
    ids = set()

    for item in board_seen:
        if isinstance(item, dict):
            notice_id = item.get("id")
            if notice_id:
                ids.add(notice_id)
        elif isinstance(item, str):
            ids.add(item)

    return ids


def make_seen_item(notice: dict) -> dict:
    return {
        "id": notice["id"],
        "title": notice.get("title", ""),
        "url": notice.get("url", ""),
        "date": notice.get("date", ""),
    }


def request_soup(url: str) -> BeautifulSoup:
    headers = {
        "User-Agent": "Mozilla/5.0 SeoulTechNoticeMonitor/1.0"
    }

    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()

    # 한글 깨짐 방지
    response.encoding = response.apparent_encoding

    return BeautifulSoup(response.text, "html.parser")


def get_notice_id(url: str) -> str:
    query = parse_qs(urlparse(url).query)

    parts = []
    for key in ["bidx", "qidx", "bnum"]:
        if key in query and query[key]:
            parts.append(f"{key}:{query[key][0]}")

    if parts:
        return "|".join(parts)

    return url


def parse_date_from_text(text: str):
    match = DATE_RE.search(text or "")
    if not match:
        return None

    year, month, day = map(int, match.groups())

    try:
        return datetime(year, month, day).date()
    except ValueError:
        return None


def make_page_url(base_url: str, page: int) -> str:
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    query["nowpage"] = [str(page)]

    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def extract_recent_notice_links(board_url: str, cutoff_date) -> list[dict]:
    unique = {}

    for page in range(1, MAX_PAGES_PER_BOARD + 1):
        page_url = make_page_url(board_url, page)
        soup = request_soup(page_url)

        dated_count = 0
        recent_count = 0

        print(f"[INFO] Fetch page {page}: {page_url}")

        for a in soup.find_all("a", href=True):
            href = a["href"]
            title = a.get_text(" ", strip=True)

            if "do=commonview" not in href:
                continue

            if not title:
                continue

            row = a.find_parent("tr") or a.find_parent("li") or a.parent
            row_text = row.get_text(" ", strip=True) if row else title

            posted_date = parse_date_from_text(row_text)

            # 날짜를 못 읽은 글은 기준을 알 수 없으므로 제외
            if posted_date is None:
                print(f"[WARN] Date not found. Skip: {title}")
                continue

            dated_count += 1

            if posted_date < cutoff_date:
                continue

            recent_count += 1

            full_url = urljoin(board_url, href)
            notice_id = get_notice_id(full_url)

            unique[notice_id] = {
                "id": notice_id,
                "title": title,
                "url": full_url,
                "date": posted_date.isoformat(),
            }

        print(
            f"[INFO] Page {page}: dated={dated_count}, recent={recent_count}"
        )

        # 이 페이지에서 날짜 있는 글이 하나도 없으면 구조가 안 맞는 것이므로 중단
        if dated_count == 0:
            break

        # 이 페이지에 최근 3개월 글이 하나도 없으면 이후 페이지는 더 오래된 글일 가능성이 높으므로 중단
        if recent_count == 0:
            print("[INFO] Older than cutoff. Stop pagination.")
            break

    return list(unique.values())


def clean_body_text(text: str) -> str:
    text = re.sub(r"\r", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def extract_detail_body(detail_url: str) -> str:
    soup = request_soup(detail_url)

    # script/style 제거
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text("\n", strip=True)
    text = clean_body_text(text)

    # 너무 앞쪽의 홈페이지 공통 메뉴 제거를 위한 대략적 컷
    start_candidates = [
        "첨부파일",
        "내용",
        "본문",
    ]

    for marker in start_candidates:
        idx = text.find(marker)
        if idx != -1:
            text = text[idx + len(marker):]
            break

    # 너무 뒤쪽의 하단 메뉴 제거
    end_candidates = [
        "\n목록",
        "\n이전글",
        "\n다음글",
        "개인정보처리방침",
        "저작권보호정책",
    ]

    for marker in end_candidates:
        idx = text.find(marker)
        if idx > 100:
            text = text[:idx]
            break

    return clean_body_text(text)


def match_keywords(title: str, body: str) -> list[str]:
    target = normalize(title + "\n" + body)

    excluded = [
        kw for kw in EXCLUDE_KEYWORDS
        if normalize(kw) in target
    ]

    if excluded:
        return []

    return [
        kw for kw in KEYWORDS
        if normalize(kw) in target
    ]


def split_text(text: str, limit: int = 3000) -> list[str]:
    if not text:
        return ["(본문 없음)"]

    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]

    return chunks


def send_telegram_message(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    api_url = f"https://api.telegram.org/bot{token}/sendMessage"

    response = requests.post(
        api_url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=20,
    )

    response.raise_for_status()


def send_test_message() -> None:
    send_telegram_message(
        "<b>[서울과기대 공지 알림 테스트]</b>\n\n"
        "Telegram Bot Token과 Chat ID 연결이 정상입니다."
    )


def notify(board_name: str, title: str, body: str, url: str, matched: list[str]) -> None:
    safe_board = html.escape(board_name)
    safe_title = html.escape(title)
    safe_body = html.escape(body)
    safe_url = html.escape(url)
    safe_keywords = html.escape(", ".join(matched))

    header = (
        "<b>[서울과기대 공지 알림]</b>\n\n"
        f"<b>게시판:</b> {safe_board}\n"
        f"<b>매칭 키워드:</b> {safe_keywords}\n\n"
        f"<b>제목:</b>\n{safe_title}\n\n"
        f"<b>링크:</b>\n{safe_url}\n\n"
        "<b>본문:</b>\n"
    )

    body_chunks = split_text(safe_body, limit=3000)

    send_telegram_message(header + body_chunks[0])

    for i, chunk in enumerate(body_chunks[1:], start=2):
        time.sleep(0.5)
        send_telegram_message(
            f"<b>[본문 계속 {i}]</b>\n\n{chunk}\n\n{safe_url}"
        )


def main() -> None:
    if os.getenv("TEST_MESSAGE") == "true":
        send_test_message()
        return

    seen = load_seen()
    first_run = not SEEN_PATH.exists() or seen == {}
    changed = False

    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    cutoff_date = today - timedelta(days=RECENT_DAYS)
    
    print(f"[INFO] Today: {today}")
    print(f"[INFO] Cutoff date: {cutoff_date}")

    for board in BOARDS:
        board_name = board["name"]
        board_url = board["url"]

        seen.setdefault(board_name, [])

        print(f"[INFO] Checking board: {board_name}")

        notices = extract_recent_notice_links(board_url, cutoff_date)
        known_ids = get_known_ids(seen[board_name])

        print(f"[INFO] Found {len(notices)} notices")

        for notice in notices:
            notice_id = notice["id"]

            if notice_id in known_ids:
                continue

            title = notice["title"]
            detail_url = notice["url"]
            posted_date = notice.get("date", "unknown")
            
            print(f"[INFO] New notice: {board_name} | {posted_date} | {title}")
            
            # 첫 실행 때는 최근 6개월 글을 seen.json에만 저장하고,
            # 상세 페이지 본문은 열지 않음
            if first_run:
                seen[board_name].append(make_seen_item(notice))
                changed = True
                continue
            
            try:
                body = extract_detail_body(detail_url)
            except Exception as e:
                body = f"(본문 추출 실패: {e})"
            
            matched = match_keywords(title, body)
            
            if matched:
                notify(board_name, title, body, detail_url, matched)
            
            seen[board_name].append(make_seen_item(notice))
            changed = True

    if changed:
        save_seen(seen)
        print("[INFO] seen.json updated")
    else:
        print("[INFO] No new notices")


if __name__ == "__main__":
    main()
