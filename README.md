# invest-monitor-starter

이 저장소는 **12개 종목**의
1) 매일 가격/거래량 등 핵심 지표 변동
2) 회사 보도자료(Press Releases) / 공시(SEC filings) 신규 항목

을 모아서 **하루 1회 이메일(또는 텔레그램)**로 받는 자동화 템플릿입니다.

> ⚠️ 투자 조언이 아닙니다. 데이터 지연/오류가 있을 수 있습니다.

## 0) 준비물
- GitHub 계정
- (선택) cron-job.org 계정
- 알림 수단 1개
  - 이메일(SMTP) 추천: Gmail App Password 또는 회사 SMTP
  - 또는 Telegram Bot

## 1) GitHub에 올리기
1. GitHub에서 New repository 생성
2. 이 폴더의 파일들을 그대로 업로드(또는 복사/붙여넣기)

## 2) GitHub Secrets 설정
Repository > Settings > Secrets and variables > Actions > **New repository secret**

### (A) 이메일로 받기
아래 Secret들을 추가하세요.

- `ALERT_METHOD` = `email`
- `EMAIL_TO` = 수신 이메일
- `EMAIL_FROM` = 발신 이메일(보통 SMTP 계정과 동일)
- `SMTP_HOST` = 예: `smtp.gmail.com`
- `SMTP_PORT` = 예: `587`
- `SMTP_USER` = SMTP 로그인 아이디(보통 이메일)
- `SMTP_PASS` = SMTP 비밀번호 (Gmail은 App Password 권장)

### (B) 텔레그램으로 받기
- `ALERT_METHOD` = `telegram`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### (공통) SEC User-Agent (권장)
SEC 데이터 요청 시 User-Agent에 연락처가 있어야 합니다.
- `SEC_USER_AGENT` = 예: `YourName your@email.com`

## 3) Actions 실행 확인(수동)
1. GitHub 탭: **Actions**
2. `Daily Investment Monitor` 워크플로우 선택
3. **Run workflow** 클릭

성공하면 메일/텔레그램이 옵니다.

## 4) 매일 자동 실행(스케줄)
`.github/workflows/daily.yml`에 cron이 들어있습니다.
GitHub Actions의 schedule은 **UTC 기준**입니다.

원하는 한국시간(KST)으로 바꾸려면:
- KST 07:30 = UTC 22:30 (전날)
- KST 08:00 = UTC 23:00 (전날)

## 5) 보도자료 소스가 안 잡히는 경우
회사 IR 사이트가 403을 주거나(봇 차단) JS로 “Loading…”만 보이는 경우가 있습니다.
그럴 땐 `config.yaml`의 `sources`에 아래 중 하나를 추가하세요.

- GlobeNewswire 조직 페이지: `https://www.globenewswire.com/search/organization/<회사명>`
- BusinessWire seed 기사(해당 회사 기사 아무거나 1개)

또는 회사 IR 사이트의 RSS가 있으면 RSS로 바꾸는 게 가장 안정적입니다.

## 6) 체크리스트 수정
`config.yaml`의 각 회사 `checklist:` 줄에
당신이 매일 확인하고 싶은 항목을 텍스트로 추가하면 됩니다.
