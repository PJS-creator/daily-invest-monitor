# invest-monitor (GitHub Discussions 버전, Dashboard + Anchors)

이 저장소는 **12개 종목**에 대해 매일 1회:

1) **매일 변하는 지표**: Close/전일대비/거래량/시총/EV(앵커 기반 계산)
2) **분기·이벤트로 변하는 앵커 지표**: Net Cash, Burn, Runway, Buyout/SOTP (config에 입력)
3) **뉴스/보도자료**: 회사 IR + GlobeNewswire + Business Wire
4) **SEC 공시(EDGAR)**

를 모아서 **GitHub Discussions의 “Daily Report” 글에 댓글로 자동 게시**하는 템플릿입니다.

- 알림은 GitHub의 **Notifications(웹/앱 푸시)** 로 받습니다.
- 실행은 GitHub Actions 워크플로우를 사용하며,
  - GitHub Actions 스케줄(UTC) 또는
  - **cron-job.org가 GitHub API를 호출해 workflow_dispatch로 트리거**
  둘 중 하나로 돌릴 수 있습니다.

> ⚠️ 투자 조언이 아닙니다. 데이터 지연/오류가 있을 수 있습니다.

---

## 0) 준비물
- GitHub 계정
- (선택) cron-job.org 계정

---

## 1) GitHub 저장소 만들기
1. GitHub > **New repository**
2. 이름 예: `invest-monitor`
3. Public/Private 아무거나 선택
4. Create repository

---

## 2) 파일 업로드
이 폴더의 파일들을 그대로 업로드합니다.

- `monitor.py`
- `config.yaml`
- `requirements.txt`
- `data/state.json`
- `.github/workflows/daily.yml`

---

## 3) Discussions 켜기 + Daily Report 글 만들기
1. Repository > **Settings**
2. **General** > Features
3. **Discussions** 체크
4. Discussions 탭으로 이동
5. 새 Discussion 생성
   - 제목: `Daily Report`
   - 카테고리: `General` (기본값)
   - 본문: 아무거나(예: "Auto daily comments will be posted here")

---

## 4) Actions 권한 설정
Repository > **Settings** > **Actions** > **General**

- Workflow permissions: **Read and write permissions** 선택
  - `data/state.json`을 커밋하기 위해 필요

---

## 5) Secrets 설정 (SEC User-Agent 권장)
Repository > **Settings** > Secrets and variables > Actions

- `SEC_USER_AGENT` (권장)
  - 예: `YourName your@email.com`

---

## 6) Actions 실행 테스트(수동)
1. **Actions** 탭
2. `Daily Investment Monitor (to Discussions)` 워크플로우
3. **Run workflow**

성공하면 Discussions의 `Daily Report` 글에 댓글이 달립니다.

---

## 7) cron-job.org로 하루 1번 자동 트리거(추천)
cron-job.org가 GitHub API로 workflow_dispatch를 호출합니다.

### 7-1) GitHub Personal Access Token 만들기
GitHub > Settings > Developer settings > Personal access tokens

- Fine-grained token 추천
- 권한(대략):
  - Repository access: 해당 repo 선택
  - Repository permissions:
    - Actions: Read and write
    - Metadata: Read (기본)

토큰을 생성하면 **한 번만** 보여주니 안전한 곳에 복사해둡니다.

### 7-2) cron-job.org Job 생성
1. cron-job.org 로그인
2. Create cronjob
3. 설정값 예시

- URL:
  `https://api.github.com/repos/<OWNER>/<REPO>/actions/workflows/daily.yml/dispatches`
- Method: `POST`
- Headers:
  - `Accept: application/vnd.github+json`
  - `Authorization: Bearer <YOUR_PAT>`
  - `X-GitHub-Api-Version: 2022-11-28`
  - `Content-Type: application/json`
- Body:
  `{ "ref": "main" }`

4. Schedule
- Timezone: `Asia/Seoul`
- 매일 원하는 시간 1회

---

## 8) “핵심 지표(Anchors)”와 촉매(Catalysts) 수정
`config.yaml`에서 회사별로 아래를 수정하면 됩니다.

### 8-1) anchors(앵커 지표)
`anchors`는 **매일 변하는 값이 아니라**, 최신 10-Q/10-K/오퍼링 기준으로 업데이트하는 값입니다.

- `net_cash_m` : Net Cash (USD million)
- `burn_m_per_month` / `burn_m_per_quarter` / `burn_m_per_year`
- `runway_months`
- `buyout_per_share` : Bear/Base/Bull (USD/share)
- `as_of` : 기준일(예: `2025-09-30`)

### 8-2) catalysts(촉매 캘린더)
`catalysts`는 **파이프라인별 촉매(임상/규제/딜/재무)** 를 적어두는 영역입니다.

- `when`: 표기용(예: `2026 Q2`, `mid-2026`)
- `date`(선택): `YYYY-MM-DD` (적으면 D-day가 계산됩니다)
- `event`: 이벤트 설명
- `importance`(선택): 1~10

---

## 9) 알림 받기(메일/텔레그램 없이)
- GitHub 모바일 앱 설치
- 저장소에서 **Watch** > `Custom` 또는 `All Activity`
- Notifications 설정에서 Discussions/Comments 알림을 켭니다.
