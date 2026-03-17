# Daily Invest Monitor 저장소 저장 업그레이드 가이드

이 업그레이드의 목적은 **Discussion 댓글만** 남기던 구조를,

- `reports/latest.md` : 사람이 읽는 전체 리포트
- `reports/latest.json` : 기계가 읽기 쉬운 구조화 데이터
- `reports/latest_pulse.md` : Pulse / Task가 읽기 쉬운 요약본
- `reports/archive/YYYY/MM/DD/...` : 실행 시각별 스냅샷

까지 함께 남기는 구조로 바꾸는 것입니다.

이렇게 바꾸면 GitHub Discussion HTML을 긁지 않아도 되고, ChatGPT Task/Pulse가 **항상 같은 파일 경로**를 읽으면 되기 때문에 훨씬 안정적입니다.

---

## 1) 바꿔야 할 파일

아래처럼 교체하세요.

- `monitor.py` → 업그레이드 버전으로 교체
- `config.yaml` → `storage:` 섹션이 포함된 버전으로 교체
- `.github/workflows/daily.yml` → 업그레이드 버전으로 교체

추가로 업로드 파일명은 반드시 아래처럼 정리하세요.

- `requirements (1).txt` → `requirements.txt`
- `daily (1).yml` → `.github/workflows/daily.yml`

---

## 2) 새로 생기는 저장 구조

실행이 성공하면 저장소에 아래가 생깁니다.

```text
reports/
  latest.md
  latest.json
  latest_pulse.md
  manifest.json
  archive/
    2026/
      03/
        17/
          20260317_083000_KST_report.md
          20260317_083000_KST_report.json
          20260317_083000_KST_pulse.md
```

의미는 다음과 같습니다.

- `latest.md`
  - 지금 시점의 전체 리포트
- `latest.json`
  - 나중에 다른 스크립트나 앱에서 재활용하기 쉬운 구조화 데이터
- `latest_pulse.md`
  - ChatGPT Task/Pulse에 바로 물리기 가장 좋은 파일
- `archive/...`
  - 실행 시점별 히스토리
- `manifest.json`
  - 최신 파일 경로 메타데이터

---

## 3) config.yaml에 추가된 옵션

`storage:` 섹션이 추가됩니다.

```yaml
storage:
  save_repo_outputs: true
  latest_aliases: true
  archive_by_run: true
  post_to_discussions: true
  reports_dir: "reports"
```

설명:

- `save_repo_outputs`
  - `reports/` 파일 생성 여부
- `latest_aliases`
  - 매번 `latest.*`를 덮어쓰기 할지
- `archive_by_run`
  - 실행 시점별 스냅샷 보관 여부
- `post_to_discussions`
  - 기존처럼 Discussion 댓글도 계속 달지 여부
- `reports_dir`
  - 출력 폴더명

---

## 4) monitor.py에서 바뀐 핵심 동작

### 4-1) Discussion 댓글과 별개로 파일 저장
기존에는 최종 결과가 Discussion 댓글에만 남았습니다.

이제는 아래 순서로 동작합니다.

1. 전체 리포트 생성
2. Pulse용 요약 리포트 생성
3. `reports/latest.*` 저장
4. `reports/archive/...` 스냅샷 저장
5. 가능하면 Discussion에도 댓글 게시
6. `data/state.json` 저장

즉, **Discussion posting이 실패해도 저장소 파일은 남도록** 바뀝니다.

### 4-2) 구조화 JSON 저장
`latest.json`에는 대략 아래 정보가 들어갑니다.

- meta
- summary
- dashboard
- companies[]
  - ticker, name, close, pct
  - alerts
  - new_press, new_sec
  - next_catalyst
  - checklist
  - anchors
  - price_target
  - errors

### 4-3) Pulse 전용 markdown 생성
`latest_pulse.md`는 길고 복잡한 Dashboard 전체 대신,

- 전체 판단
- Must Watch
- Ignore / Noise
- Source Files

중심으로 짧게 만듭니다.

---

## 5) 워크플로우에서 바뀐 점

### 5-1) 중복 실행 방지
`concurrency`를 넣어서 이전 실행이 안 끝났을 때 겹쳐 돌지 않게 했습니다.

### 5-2) 12시간 간격 예시 반영
예시 워크플로우는 아래 두 시각 기준입니다.

- 08:30 KST
- 20:30 KST

원하는 시각이 다르면 cron만 바꾸면 됩니다.

### 5-3) reports 폴더까지 커밋
기존에는 `data/state.json`만 커밋했지만,
이제는 아래 둘 다 커밋합니다.

- `data/state.json`
- `reports/`

그래야 최신 리포트가 저장소에 실제로 남습니다.

### 5-4) optional backup artifact
추가로 Actions artifact도 올리게 해두었습니다.

이건 필수는 아니지만,
리포지터리 저장 외에 실행 결과를 한 번 더 백업하는 의미가 있습니다.

---

## 6) 실제 적용 순서

### A. 로컬에서 파일 정리
저장소 루트를 아래처럼 맞춥니다.

```text
monitor.py
config.yaml
requirements.txt
data/state.json
.github/workflows/daily.yml
```

### B. `data/state.json`가 없으면 생성
```json
{
  "last_run": null,
  "press_seen": {},
  "sec_seen": {},
  "discussion_id": null
}
```

### C. GitHub Settings 확인
Repository → Settings → Actions → General

- Workflow permissions: **Read and write permissions**

### D. Secret 확인
Repository → Settings → Secrets and variables → Actions

- `SEC_USER_AGENT`
  - 예: `YourName your@email.com`

### E. 수동 테스트
Actions 탭 → 워크플로우 → Run workflow

성공 후 확인할 것:

1. Discussion 댓글이 달렸는지
2. `reports/latest.md` 생성됐는지
3. `reports/latest.json` 생성됐는지
4. `reports/latest_pulse.md` 생성됐는지
5. `reports/archive/...` 생성됐는지

---

## 7) Pulse / Task 연결 방법

가장 안정적인 입력원은 이제 Discussion URL이 아니라 아래 둘 중 하나입니다.

1. `reports/latest_pulse.md`
2. `reports/latest.json`

추천은 `latest_pulse.md`입니다.

### 추천 프롬프트 예시

```text
매일 오전 9시 KST에 저장소의 reports/latest_pulse.md를 확인해.
지난 실행 이후 새로 반영된 변화만 기준으로,
내가 알아야 할 내용만 최대 5개까지 알려줘.

우선순위:
1) 신규 SEC
2) 비정상 주가/거래량 알림
3) 다음 촉매 일정 변화
4) 중요한 PR

형식:
- [티커] 무엇이 바뀌었나
- 왜 중요한가
- 내가 확인할 다음 포인트

중요 변화가 없으면 '오늘은 즉시 대응할 내용 없음'만 말해줘.
```

---

## 8) 추천 운영 방식

### 가장 추천
- 저장소에는 `latest_pulse.md`를 남긴다.
- Task/Pulse는 그 파일만 읽는다.
- Discussion은 사람 눈으로 확인하는 보조 채널로만 둔다.

### 이유
Discussion 페이지는 GitHub UI 텍스트와 댓글 HTML이 섞여서,
자동 읽기 입력원으로는 일관성이 떨어질 수 있습니다.
반면 `latest_pulse.md`는 매번 같은 구조로 저장되므로 훨씬 안정적입니다.

---

## 9) 추가로 더 개선하고 싶다면

다음도 붙일 수 있습니다.

- `latest_changes_only.md` 별도 생성
- 티커별 중요도 점수 산식 추가
- SEC filing type(8-K, S-3, 10-K 등) 자동 분류
- PR 제목 키워드 기반 규제/임상/재무 태깅
- `reports/history.csv` 누적 로그 생성
- 특정 티커만 별도 alert 파일 생성

이 정도까지 가면 ChatGPT Task가 거의 바로 actionable summary를 만들 수 있습니다.
