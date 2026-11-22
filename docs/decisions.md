# 결정 기록

최신이 위. 형식: 날짜/시각 · 결정 · 이유 · 버린 대안.

- 일 11/22 02:20 · `_fast_max_nodes` 경량 검증 + CPU 예산 기반 TPC 실검증(`REAL_BUDGET_S`)·`dbg_sql` 단계 + `_LazyFuture` 지연 실행 + `dict_entry_change` AST 딕셔너리 전체 차분 + `probe_change_impact` 축소 + 무차원 amd64 게이트 · `c8cd459`(140.7 s)에서 남은 큰 초과 요인인 `test_max_nodes`(28.8 s), 다수 쿼리 불일치 TPC(21.5 s), import probe 전체 폴백, amd64 임계치 보정, 실패 fixture CPU를 모두 줄이면서 0 miss 유지 · `sqlglot/executor/*`에 30행 `q_lines` 적용(전체 CSV에서만 타는 분기 누락으로 miss 위험 확인 후 폐기), `fast_max_nodes`에서 `parser.py` 관련 가드 완화(보수적 유지)
- 토 11/21 22:55 · `blast/subcov.py` 세부 항목 커버리지(SQL fixture·`__subtests__`·def-use `__blocks__`·dialect `__vcalls__`) + 2패스 slack-aware 검증 게이트 + `PROXY`를 `GIANT_TESTS`로 한정 + `probe.py` 메모이제이션 · 검증 CPU 대부분이 바뀐 줄을 안 지나는 서브케이스 반복 실행과 이미 확정된 워커 비용 아래의 저효율 테스트 실행에서 나와서, 테스트 내부 항목 단위로 좁히고 한계 이득이 없는 검증을 건너뛰어 검증 117개 시뮬레이션 395.1 s → 336.7 s, CPU 65.8 s → 38.2 s(miss 0)로 단축 · `test_optimize_in_nulls` DuckDB 생략(실측상 DuckDB 비중이 29%뿐이라 이득 대비 리스크 큼), `sole_followup` 시 게이트 우회(검증셋 CPU +4.6 s 역효과)
- 토 11/21 18:15 · pytest 대신 직접 `unittest.TestCase` 러너 + 빌드 시점 실제 실행시간 보정(`blast/bench.py`) + 단계/쿼리 필터링 TPC 다이제스트 · sqlglot 테스트 1,620개 전부가 표준 unittest라서 pytest 기동비(700 ms → 32 ms)를 없애고, 실제 CPU 시간이 기록 시간보다 3~85배 짧은 테스트들을 정확히 골라 검증할 수 있음 · pytest 유지(CPU 병목), TPC 플랜 해시 생략(30행 미니 테이블만으로 전체 테이블 일치를 보장 못 해 miss 위험)
- 토 11/21 14:50 · 정적 후보 중 짧은 테스트(1.5초 이하)만 컨테이너 안에서 실제로 돌려 보고, 통과하면 뺌. 통과할 확률 × 아끼는 비용이 CPU보다 클 때만 · 처음 보는 종류의 변경도 실제 실행이 잡아 줌 · 모든 후보 확인(CPU 16 s로 손해), 긴 테스트 확인(돌리는 비용이 넣는 비용과 같음)
- 토 11/21 14:50 · tpcds를 쿼리 단위로 잘라 확인하는 건 보류 · sample에서 tpcds가 후보인데 안 깨지는 두 커밋 모두 실행되는 쿼리 57개 중 56~57개가 바뀐 줄을 지나서 자를 게 없음 · 쿼리별 coverage 맵(tools/covmap/trace_queries.py)
- 토 11/21 12:55 · Blast Radius v1: 고정 커밋에서 테스트별 함수 본문 라인 coverage(테스트마다 fork, spawn 풀 워커까지 추적) + 연 파일 + 생성한 클래스 맵을 커밋하고, 패치를 AST로 분석해 "바뀐 문장을 실행한 테스트"만 고름 · 맵은 규칙상 허용(coverage/imports/fixtures) · 패치→실패 테이블(금지), 뮤테이션으로 만든 맵(금지 소지)
- 토 11/21 · 검증용 커밋은 tools/mutate/gen.py로 만들고 label.py로 실제 테스트를 돌려 정답을 만듦(데이터는 커밋 안 함). 라벨러는 공식 샘플 정답을 재현하는지로 검증 · 샘플 30개만으로는 과적합
- 토 11/21 10:40 · 공유 통로는 GitHub repo, 맥락은 `docs/` 파일로 공유 · 두 사람이 다른 컴퓨터에서 작업함 · 실시간 공동 편집
- 토 11/21 · **트랙: RWX Blast Radius** · 파이널만 점수에 반영돼서 늦게 시작해도 불리하지 않고, 노트북과 로컬 채점기로 진행 가능 · Machina(인프라 부담), Shopping(포화), Mech Royale(운 요소)
- 토 11/21 · 이 repo(`seungminnam/buildathon`)를 public으로 전환해서 제출용으로 사용. Dockerfile은 repo 루트에 둠 · 제출하려면 public이어야 함 · 기록용과 제출용 repo 분리
