# invest-monitor (GitHub Discussions 버전)

이 저장소는 **12개 종목**에 대해 매일 1회:

1) 가격/거래량 등 핵심 지표 변동
2) 회사 뉴스/보도자료(Press Releases)
3) SEC 공시(EDGAR)

를 모아서 **GitHub Discussions의 "Daily Report" 글에 댓글로 자동 게시**하는 템플릿입니다.

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

> `.github` 폴더가 숨김 처리되는 경우가 있어요.
> 그럴 땐 GitHub 웹에서 **Add file > Create new file** 로
> 파일명에 `.github/workflows/daily.yml` 를 입력해서 생성한 뒤
> 내용을 붙여넣어도 됩니다.

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
  (state.json 커밋을 위해 필요)

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

## 8) 종목/체크리스트 수정
`config.yaml`에서 회사별로 아래를 수정하면 됩니다.

- `ticker`, `name`
- `sources` (회사 IR, GlobeNewswire/BusinessWire 검색 URL)
- `checklist` (내가 매일 확인할 항목)

---

