# 실험·제출 로그

최신이 위. 제출이 아닌 로컬 실험도 기록.

| 시각 (EST) | 누가 | 커밋 | 무엇을 바꿨나 | 로컬 점수 | 서버 점수 | 메모 |
|---|---|---|---|---|---|---|
| 일 02:25 | ryan | (v9) | `_fast_max_nodes` 경량 검증(20 ms) + CPU 예산 기반 TPC 실검증(`REAL_BUDGET_S`)·`py_core_lines` 기반 `dbg_sql` 단계 + `_LazyFuture` 지연 실행(`as_completed`/`wait` 패치) + `dict_entry_change` AST 딕셔너리 차분(중복 키·`**DictComp`·`EXPRESSION_METADATA`) + `probe_change_impact` 축소 + 무차원 amd64 검증 게이트·`budget_ctx` 보정 + 중복 `tpc_check` 제거 | arm64: sample **94.35 s**(정답 일치) + CPU 8.6 s, val 117개 **336.54 s** + CPU 42.4 s, 0 miss. amd64: sample **98.58 s**, val **343.90 s**(유효 116개 초과 +12.84 s, 빌드 122 s), 전체 147커밋 0 miss | | v8 amd64 대비 val 유효 초과 -16.0 s, arm64 val CPU -3.5 s, 스트레스 27건 0 miss |
| 토 23:00 | ryan | c8cd459 | 항목 단위 sub-coverage(`blast/subcov.py`: 229개 테스트 SQL fixture·`__subtests__`·def-use `__blocks__`·dialect `__vcalls__` + 캐시된 Dialect 추적) + 2패스 slack-aware 검증 게이트 + `PROXY`를 `GIANT_TESTS`로 한정 + 단독 잔여 테스트 재검증 + `probe.py` 메모이제이션·사전 워밍업 + `impact.py` SQL fixture/AST/전역 지연 캐시 가드 | sample **94.35 s**(정답 일치) + CPU 8.7 s, 0 miss. 검증 117개 **336.66 s**(정답 303.4) + CPU 38.2 s, 0 miss. amd64 전체 147커밋 0 miss(빌드 97 s) | **140.7** (시뮬 126.75 + CPU 13.96, miss 0) | v7(164.2) 대비 -23.5 s (시뮬 -8.25 s, CPU -15.24 s) |
| 토 21:17 | ryan | f7708a3 | v7 제출 (직접 unittest 러너 + InlineExecutor + 경량 TestExecutor + `blast/bench.py` + TPC 다이제스트 O(N) 플랜 키·단계/쿼리 필터 + `probe_base` 선계산 + `gc.freeze()`) | sample **94.35 s**(정답 하한 일치) + CPU 11.3 s, 0 miss. 검증 117개 **395.12 s**(정답 303.4) + CPU 65.8 s, 0 miss | **164.2** (시뮬 135.0 + CPU 29.2, miss 0) | v4(203.1) 대비 -38.9 s, v6(208.3) 대비 -44.1 s |
| 토 17:23 | ryan | 7f3e94a | v6 제출 | sample 108.69 s, 검증 117개 533.2 s | **208.3** (시뮬 151.6 + CPU 56.7, miss 0) | 시뮬은 -20.8 s 줄었지만 CPU가 +26.0 s 늘어 총점 악화 |
| 토 17:10 | ryan | (v6) | import 에러가 난 모듈을 쓰는 테스트로만 좁힘, "수집 때 import"라는 이유의 전체 폴백을 probe로 확인, 클래스 테이블 한 항목 수정은 그 항목을 쓰는 테스트로 좁힘 | sample 108.69 s, 0 miss. 검증 117개 0 miss, 533.2 s (정답 303.4) | | |
| 토 17:10 | ryan | (v5) | TPC 다이제스트: tpcds/tpch 쿼리마다 파싱·최적화·플랜·생성 코드·작은 테이블 실행 결과를 원본과 비교해서 같으면 긴 TPC 테스트를 뺌. 원본 값은 빌드 때 만듦 | sample 112.05 s, 0 miss. 검증 110개 0 miss, 545 s (정답 291) | | amd64 이미지에서 빌드 때 값과 실행 때 값이 일치하는 것 확인 |
| 토 16:39 | ryan | 7550788 | v4 제출 | sample 112.05 s | **203.1** (시뮬 172.4 + CPU 30.7, miss 0) | |
| 토 16:45 | ryan | (v4) | import probe(import 때 상태가 바뀌었는지 비교해서 "전체 테스트" 폴백을 좁힘) + 파일별 proxy(긴 테스트를 대신 판단하는 건 같은 파일 때문에 후보가 된 짧은 테스트만) | sample 112.05 s, 0 miss. 검증 73개 0 miss, 477.4 s (정답 202.5) | | 검증에서 v3는 miss 1(bv070013) |
| 토 16:21 | ryan | 9eeb5ca | v3 제출 | sample 112.05 s | **232.7** (시뮬 205.0 + CPU 27.7, miss 0) | sample만큼 안 줄었다. 연습 세트에 "전체 테스트" 폴백이 많은 듯 |
| 토 16:25 | ryan | (v3) | 싼 후보로 긴 테스트 대신 판단 + 첫 실패에서 멈춤 + 맵 수정(한 줄 lambda, Expr 클래스, import 때 만들어지는 클래스) | sample 112.05 s + CPU 약 15 s(arm64), 0 miss. 검증 33개 0 miss | | 검증 세트 정답 대비 272.9 vs 88: 대부분 "전체 테스트" 폴백 |
| 토 16:05 | ryan | - | 새 라벨러(tools/label_fast.py) 검증: sample 8개 공식 정답과 전부 일치 | - | - | 커밋당 약 110~170 s |
| 토 15:14 | ryan | 3bd056a | 첫 서버 제출 | sample 158.93 s | **266.0** (시뮬 229.4 + CPU 36.6, miss 0) | 연습 세트 40커밋. 1등 121.8 |
| 토 15:10 | ryan | 3171f51 | history 정리. 새로 clone해서 캐시 없이 amd64 빌드 34초 | sample 158.93 s, 0 miss | | |
| 토 14:50 | ryan | (v2) | v1 위에 실행 확인 추가: 짧은 후보를 컨테이너 안에서 patch된 트리에 대고 돌려서 통과한 건 뺌. fixture만 바뀐 커밋은 확인 안 함 | sample 158.93 s + 확인 CPU 2.3 s, 0 miss | | 확인을 전부 하면 CPU 16 s로 오히려 손해였음 |
| 토 12:55 | ryan | (v1) | Blast Radius v1: 테스트별 coverage 맵(fork-per-test, spawn 워커 포함) + 패치 정적 분석 + LPT 패킹 | sample 165.40 s, 0 miss | | oracle 94.35; Docker make sample 통과 |
| 토 11:30 | ryan | - | Blast Radius 스타터 그대로 make sample | sample 1210.47 s, 0 miss | | 서버 baseline 1614.1 (연습셋 ~40커밋) |
| 토 10:40 | ryan | - | 문서 초기화, 브리프 저장 | - | - | 리더보드 스냅샷은 01-tracks.md |
