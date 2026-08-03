import html
import json
import os
import re
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
        "url": "https://www.seoultech.ac.kr/service/info/job/?allboard=true&searchtype=-1&searchtext=",
    },
    {
        "name": "대학공지사항",
        "url": "https://www.seoultech.ac.kr/service/info/notice/?allboard=true&searchtype=-1&searchtext=",
    },
    {
        "name": "장학공지",
        "url": "https://www.seoultech.ac.kr/service/info/janghak/?allboard=true&searchtype=-1&searchtext=",
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
    "해커톤",
    "메이커장비교육",
    "3D프린터",
    "특강",
    "강연",
    "세미나",
    "콜로키움",
]

EXCLUDE_KEYWORDS = [
    "지급완료",
    "마감",
]

ALWAYS_NOTIFY_BOARDS = {
    "장학공지",
}

SEEN_PATH = Path("seen.json")
RECENT_DAYS = 90
MAX_PAGES_PER_BOARD = 1

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

    query["allboard"] = ["true"]
    query["nowpage"] = [str(page)]
    query.setdefault("searchtype", ["-1"])
    query.setdefault("searchtext", [""])

    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


GENERIC_TITLE_TOKENS = {
    "더보기", "더보기+", "더보기 +", "+", "more", "more+",
    "자세히", "자세히보기", "상세보기", "바로가기", "view", "detail",
    "read more", "목록",
}


def _is_generic_title(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "").lower()

    if not compact:
        return True

    if compact in {t.replace(" ", "").lower() for t in GENERIC_TITLE_TOKENS}:
        return True

    # 화살표/기호 등 글자가 전혀 없는 경우 (예: "»", "›")
    if not re.search(r"[0-9a-zA-Z가-힣]", compact):
        return True

    return False


def _pick_best_title(candidates: list[str]) -> str:
    ordered = []
    for candidate in candidates:
        candidate = candidate.strip()
        if candidate and candidate not in ordered:
            ordered.append(candidate)

    non_generic = [c for c in ordered if not _is_generic_title(c)]
    if non_generic:
        return max(non_generic, key=len)

    return ordered[0] if ordered else ""


def extract_recent_notice_links(board_url: str, cutoff_date) -> list[dict]:
    page_url = make_page_url(board_url, 1)
    soup = request_soup(page_url)

    print(f"[INFO] Fetch latest page only: {page_url}")

    # 같은 게시글을 가리키는 <a> 태그가 여러 개(썸네일, "더보기" 아이콘 등)일 수 있으므로
    # 우선 게시글 단위로 후보 텍스트를 모아둔 뒤 가장 적절한 제목을 고른다.
    grouped: dict[str, dict] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]

        if "do=commonview" not in href:
            continue

        full_url = urljoin(board_url, href)
        notice_id = get_notice_id(full_url)

        candidates = []

        attr_title = (a.get("title") or "").strip()
        if attr_title:
            candidates.append(attr_title)

        text = a.get_text(" ", strip=True)
        if text:
            candidates.append(text)

        if not candidates:
            continue

        row = a.find_parent("tr") or a.find_parent("li") or a.parent

        entry = grouped.setdefault(notice_id, {"url": full_url, "row": row, "candidates": []})
        entry["candidates"].extend(candidates)

    dated_count = 0
    recent_count = 0
    unique = {}

    for notice_id, entry in grouped.items():
        row = entry["row"]
        row_text = row.get_text(" ", strip=True) if row else ""

        posted_date = parse_date_from_text(row_text)

        if posted_date is None:
            continue

        dated_count += 1

        if posted_date < cutoff_date:
            continue

        recent_count += 1

        title = _pick_best_title(entry["candidates"])
        if not title:
            continue

        unique[notice_id] = {
            "id": notice_id,
            "title": title,
            "url": entry["url"],
            "date": posted_date.isoformat(),
        }

    print(f"[INFO] Latest page: dated={dated_count}, recent={recent_count}")
    print(f"[INFO] Total unique recent notices: {len(unique)}")

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

    # 너무 뒤쪽의 하단 메뉴/이전글·다음글 제거
    # 표기 방식이 게시판마다 달라("\n이전글", "이전 내용이 없습니다" 등) 최대한 다양한
    # 마커를 모아둔 뒤, 실제로 텍스트에 등장한 것 중 가장 먼저 나오는 위치에서 자른다.
    end_candidates = [
        "\n목록",
        "\n이전글",
        "\n다음글",
        "\n이전 글",
        "\n다음 글",
        "이전 내용이 없습니다",
        "다음 내용이 없습니다",
        "이전글이 없습니다",
        "다음글이 없습니다",
        "개인정보처리방침",
        "저작권보호정책",
    ]

    cut_idx = None
    for marker in end_candidates:
        idx = text.find(marker)
        if idx > 100 and (cut_idx is None or idx < cut_idx):
            cut_idx = idx

    if cut_idx is not None:
        text = text[:cut_idx]

    return clean_body_text(text)


def match_keywords(title: str) -> list[str]:
    # 이전글/다음글 제목 등 본문에 섞여 들어오는 다른 게시글 정보로
    # 오탐지되지 않도록 오직 해당 게시물의 제목만으로 판단한다.
    target = normalize(title)

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


GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

SUMMARY_PROMPT = (
    "다음은 대학교 공지사항 게시글이다. 이 글을 읽고 실제로 확인이 필요한 핵심 정보만 "
    "불릿 목록으로 정리해줘.\n"
    "- 지원자격/신청대상, 지원금액/혜택, 신청기간/마감일, 일시, 장소, 문의처 등 게시글에 "
    "실제로 등장하는 항목만 포함하고, 본문에 없는 내용은 만들어내지 마.\n"
    "- 각 줄은 '- 항목: 내용' 형식으로 작성하고 최대 6줄 이내로 작성해.\n"
    "- 마지막 줄에는 전체 내용을 한 문장으로 요약해서 추가해.\n"
    "- 다른 설명이나 인사말 없이 목록만 출력해.\n\n"
    "[제목]\n{title}\n\n[본문]\n{body}"
)


def summarize_with_ai(title: str, body: str) -> str | None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or not body:
        return None

    api_url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    )
    prompt = SUMMARY_PROMPT.format(title=title, body=body[:6000])

    try:
        response = requests.post(
            api_url,
            params={"key": api_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 500},
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        candidates = data.get("candidates") or []
        if not candidates:
            return None

        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts).strip()

        return text or None
    except Exception as e:
        print(f"[WARN] AI 요약 실패: {e}")
        return None


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
    safe_url = html.escape(url)
    safe_keywords = html.escape(", ".join(matched))

    summary = summarize_with_ai(title, body)
    summary_text = (
        html.escape(summary) if summary
        else "(AI 요약 불가 - 링크에서 본문을 직접 확인해주세요)"
    )

    message = (
        "<b>[서울과기대 공지 알림]</b>\n\n"
        f"<b>게시판:</b> {safe_board}\n"
        f"<b>매칭 키워드:</b> {safe_keywords}\n\n"
        f"<b>제목:</b>\n{safe_title}\n\n"
        f"<b>AI 요약:</b>\n{summary_text}\n\n"
        f"<b>링크:</b>\n{safe_url}"
    )

    send_telegram_message(message)


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
            
            if board_name in ALWAYS_NOTIFY_BOARDS:
                matched = ["장학공지 전체 알림"]
            else:
                matched = match_keywords(title)

            if matched:
                try:
                    body = extract_detail_body(detail_url)
                except Exception as e:
                    body = f"(본문 추출 실패: {e})"

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
