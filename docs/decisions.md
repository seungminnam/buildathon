# 결정 기록

최신이 위. 형식: 날짜/시각 · 결정 · 이유 · 버린 대안.

- 토 11/21 12:55 · Blast Radius v1: 고정 커밋에서 테스트별 함수 본문 라인 coverage(테스트마다 fork, spawn 풀 워커까지 추적) + 연 파일 + 생성한 클래스 맵을 커밋하고, 패치를 AST로 분석해 "바뀐 문장을 실행한 테스트"만 고름 · 맵은 규칙상 허용(coverage/imports/fixtures) · 패치→실패 테이블(금지), 뮤테이션으로 만든 맵(금지 소지)
- 토 11/21 · 검증용 커밋은 tools/mutate/gen.py로 만들고 label.py로 실제 테스트를 돌려 정답을 만듦(데이터는 커밋 안 함). 라벨러는 공식 샘플 정답을 재현하는지로 검증 · 샘플 30개만으로는 과적합
- 토 11/21 10:40 · 공유 통로는 GitHub repo, 맥락은 `CLAUDE.md`와 `docs/` 파일로 공유 · 두 사람의 계정과 컴퓨터가 달라서 세션 공유가 안 됨 · Orca/Claude 세션 공유
- 토 11/21 · **트랙: RWX Blast Radius** · 파이널만 점수에 반영돼서 늦게 시작해도 불리하지 않고, 노트북과 로컬 채점기로 진행 가능 · Machina(인프라 부담), Shopping(포화), Mech Royale(운 요소)
- 토 11/21 · 이 repo(`seungminnam/buildathon`)를 public으로 전환해서 제출용으로 사용. Dockerfile은 repo 루트에 둠 · 제출하려면 public이어야 함 · 기록용과 제출용 repo 분리
