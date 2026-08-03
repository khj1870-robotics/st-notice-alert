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
from google import genai
from google.genai import types


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
DATE_CELL_RE = re.compile(r"^\s*(20\d{2})[-.](\d{1,2})[-.](\d{1,2})\s*$")


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


def extract_posted_date(row):
    """목록 행의 날짜 전용 셀에서 게시일을 읽는다."""
    if row is None:
        return None

    # 제목이나 신청기간에 포함된 날짜를 게시일로 오인하지 않도록,
    # 내용 전체가 날짜인 표 셀만 인정한다.
    for cell in row.find_all(["td", "th"]):
        text = cell.get_text(" ", strip=True)
        match = DATE_CELL_RE.fullmatch(text)
        if not match:
            continue

        year, month, day = map(int, match.groups())
        try:
            return datetime(year, month, day).date()
        except ValueError:
            continue

    # 표가 아닌 목록형 게시판은 <time datetime="...">을 사용할 수 있다.
    time_tag = row.find("time", datetime=True)
    if time_tag:
        return parse_date_from_text(time_tag.get("datetime", ""))

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
    "read more", "목록", "새창열림", "새 창 열림",
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


def _title_cell_priority(anchor) -> int:
    cell = anchor.find_parent(["td", "th"])
    if cell is None:
        return 0

    classes = " ".join(cell.get("class", []))
    cell_hint = normalize(f"{classes} {cell.get('id', '')}")
    return 10 if re.search(r"title|subject|sbj|tit|제목", cell_hint) else 0


def _pick_best_title(candidates: list[tuple[int, str]]) -> str:
    usable = []
    seen = set()

    for priority, candidate in candidates:
        candidate = candidate.strip()
        if not candidate or candidate in seen or _is_generic_title(candidate):
            continue
        seen.add(candidate)
        usable.append((priority, candidate))

    if not usable:
        return ""

    # 구조상 제목 셀에 있는 링크를 우선하고, 같은 우선순위에서만 긴 제목을 고른다.
    return max(usable, key=lambda item: (item[0], len(item[1])))[1]


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
        cell_priority = _title_cell_priority(a)

        attr_title = (a.get("title") or "").strip()
        if attr_title:
            candidates.append((cell_priority + 1, attr_title))

        text = a.get_text(" ", strip=True)
        if text:
            candidates.append((cell_priority + 2, text))

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
        posted_date = extract_posted_date(row)

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


# "-latest" 별칭은 구글이 새 모델을 낼 때마다 자동으로 최신 flash 모델을 가리키도록
# 유지해주므로, 특정 버전(예: gemini-2.0-flash)을 고정해서 나중에 구버전 취급되는 것을 피한다.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-flash-latest"

SUMMARY_PROMPT = (
    "다음은 대학교 게시판 공지 게시글이다. 아래 JSON 스키마에 맞는 JSON 객체 하나만 출력해. "
    "코드블록 표시(```)나 다른 설명 문장 없이 순수 JSON만 출력해.\n\n"
    "스키마 필드 설명:\n"
    "- eligibility_grade4: 이 글이 학생 지원(장학금/모집 등) 관련 내용일 때, 4학년(졸업학년)도 "
    "지원 가능한지 여부. 학년 제한이 아예 언급되어 있지 않으면 4학년을 포함한 모든 학년이 지원 "
    "가능한 것으로 보고 \"yes\"로 판단해. 본문에 명시는 있는데 4학년 포함 여부가 애매하면 "
    "\"unclear\". 지원/모집과 무관한 공지면 \"n/a\".\n"
    "- living_expense: 지원금/장학금이 생활비 명목(생활비 지원, 생활장학금 등)이면 \"yes\", "
    "등록금·활동비·상금 등 생활비 목적이 아니면 \"no\". 금전적 지원이 없는 공지면 \"n/a\".\n"
    "- residency_seoul_jeju: 거주지/출신지 제한이 있을 때 서울 거주자 또는 제주도 출신자가 "
    "지원 가능한지 여부. 지역 제한이 아예 언급되어 있지 않으면 서울/제주 모두 포함되는 것으로 "
    "보고 \"yes\". 서울/제주가 아닌 특정 지역(예: 울산, 부산 등)으로만 한정되어 있으면 \"no\". "
    "애매하면 \"unclear\". 이 공지에 지역 제한 개념 자체가 해당하지 않으면 \"n/a\".\n"
    "- income_bracket3: 학자금 지원구간(소득분위) 기준으로 3분위 학생이 지원 가능한지 여부. "
    "소득분위 기준이 아예 언급되어 있지 않으면 지원 가능한 것으로 보고 \"yes\". 3분위가 명백히 "
    "제외되는 기준(예: 1~2분위만 해당, 기초생활수급자만 등)이면 \"no\". 애매하면 \"unclear\". "
    "소득분위 개념이 이 공지에 해당하지 않으면 \"n/a\".\n"
    "- gpa_cutoff: 성적(평점) 커트라인이 본문에 명시되어 있으면 그 값을 그대로 문자열로 "
    "(예: \"직전학기 평점 3.0/4.5 이상\"). 본문에 성적 기준이 아예 없으면 빈 문자열.\n"
    "- documents: 신청 시 제출해야 하는 서류/자료 이름의 배열. 본문에 명시된 것만 담고, "
    "없으면 빈 배열.\n"
    "- summary: 핵심을 아주 짧은 보고서 헤드라인처럼 한 구절로 요약(완결된 문장 아니어도 됨). "
    "게시글 제목을 그대로 반복하지 마.\n"
    "- details: label/value 쌍의 배열. 지원자격, 지원금액, 신청기간, 일시, 장소, 문의처 등 "
    "본문에 실제로 등장하는 핵심 항목만 최대 6개까지. 각 value도 최대한 짧게. 본문에 없는 "
    "내용은 만들어내지 마.\n\n"
    "[제목]\n{title}\n\n[본문]\n{body}"
)

SUMMARY_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "eligibility_grade4": {"type": "string", "enum": ["yes", "no", "unclear", "n/a"]},
        "living_expense": {"type": "string", "enum": ["yes", "no", "n/a"]},
        "residency_seoul_jeju": {"type": "string", "enum": ["yes", "no", "unclear", "n/a"]},
        "income_bracket3": {"type": "string", "enum": ["yes", "no", "unclear", "n/a"]},
        "gpa_cutoff": {"type": "string"},
        "documents": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "details": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["label", "value"],
            },
        },
    },
    "required": ["summary"],
}

_gemini_client = None


def _get_gemini_client():
    global _gemini_client

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=api_key)

    return _gemini_client


def summarize_with_ai(title: str, body: str) -> dict | None:
    if not body:
        return None

    client = _get_gemini_client()
    if client is None:
        return None

    prompt = SUMMARY_PROMPT.format(title=title, body=body[:6000])

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SUMMARY_RESPONSE_SCHEMA,
            ),
        )
        text = (response.text or "").strip()
        if not text:
            return None
        return json.loads(text)
    except Exception as e:
        print(f"[WARN] AI 요약 실패: {e}")
        return None


CHECK_ICONS = {"yes": "✅", "no": "❌", "unclear": "❓"}
CHECK_LABELS = {
    "eligibility_grade4": "4학년 지원 가능",
    "living_expense": "생활비성 지원금",
    "residency_seoul_jeju": "서울/제주 거주(출신) 가능",
    "income_bracket3": "소득 3분위 지원 가능",
}
CHECK_FIELDS = (
    "eligibility_grade4",
    "living_expense",
    "residency_seoul_jeju",
    "income_bracket3",
)


def format_ai_summary(parsed: dict) -> str | None:
    sections = []

    checks = []
    for field in CHECK_FIELDS:
        value = parsed.get(field)
        icon = CHECK_ICONS.get(value)
        if icon:
            checks.append(f"{icon} {CHECK_LABELS[field]}")

    gpa_cutoff = (parsed.get("gpa_cutoff") or "").strip()
    if gpa_cutoff:
        checks.append(f"🎯 성적컷: {html.escape(gpa_cutoff)}")

    if checks:
        sections.append("\n".join(checks))

    summary = (parsed.get("summary") or "").strip()
    if summary:
        sections.append(f"📝 {html.escape(summary)}")

    documents = [html.escape(str(d).strip()) for d in (parsed.get("documents") or []) if str(d).strip()]
    if documents:
        sections.append("📎 <b>신청자료:</b> " + ", ".join(documents))

    detail_lines = []
    for item in parsed.get("details") or []:
        label = html.escape(str(item.get("label", "")).strip())
        value = html.escape(str(item.get("value", "")).strip())
        if label and value:
            detail_lines.append(f"• <b>{label}:</b> {value}")
    if detail_lines:
        sections.append("\n".join(detail_lines))

    if not sections:
        return None

    return "\n\n".join(sections)


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

    parsed = summarize_with_ai(title, body)
    summary_block = format_ai_summary(parsed) if parsed else None
    if not summary_block:
        summary_block = "(AI 요약 불가 - 링크에서 본문을 직접 확인해주세요)"

    message = (
        "📢 <b>서울과기대 공지 알림</b>\n\n"
        f"🏷 {safe_board} · 🔑 {safe_keywords}\n\n"
        f"<b>{safe_title}</b>\n\n"
        f"🤖 <b>AI 요약</b>\n{summary_block}\n\n"
        f"🔗 {safe_url}"
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
