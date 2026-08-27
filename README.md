# st-notice-alert
학교 공지사항 키워드 알림

## 필요한 GitHub Secrets

| Secret | 설명 |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | 알림을 보낼 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 알림을 받을 채팅 ID |
| `GEMINI_API_KEY` | 공지 본문 AI 요약에 사용하는 Gemini API 키 (선택) |

`GEMINI_API_KEY`가 없으면 AI 요약 없이 제목/링크만 알림으로 전송됩니다.

Gemini API 키는 [Google AI Studio](https://aistudio.google.com/apikey)에서 무료로 발급받을 수 있습니다.
무료 티어 한도 내에서 하루 몇 건 수준의 요약은 비용 없이 사용 가능합니다.
