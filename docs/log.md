# 실험·제출 로그

최신이 위. 제출이 아닌 로컬 실험도 기록.

| 시각 (EST) | 누가 | 커밋 | 무엇을 바꿨나 | 로컬 점수 | 서버 점수 | 메모 |
|---|---|---|---|---|---|---|
| 토 15:20 | worker | (v2) | 싼 후보 테스트를 컨테이너에서 직접 돌려 실패만 남김(오버레이+unittest), no-op 판정 가속 | sample 156.40 s, 0 miss | | 에뮬레이션 CPU 1.4+2.7 s |
| 토 12:55 | worker | (v1) | Blast Radius v1: 테스트별 coverage 맵(fork-per-test, spawn 워커 포함) + 패치 정적 분석 + LPT 패킹 | sample 165.40 s, 0 miss | | oracle 94.35; Docker make sample 통과 |
| 토 11:30 | worker | - | Blast Radius 스타터 그대로 make sample | sample 1210.47 s, 0 miss | | 서버 baseline 1614.1 (연습셋 ~40커밋) |
| 토 10:40 | ryan jr. | - | 문서 초기화, 브리프 저장 | - | - | 리더보드 스냅샷은 01-tracks.md |
