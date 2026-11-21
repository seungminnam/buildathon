# 실험·제출 로그

최신이 위. 제출이 아닌 로컬 실험도 기록.

| 시각 (EST) | 누가 | 커밋 | 무엇을 바꿨나 | 로컬 점수 | 서버 점수 | 메모 |
|---|---|---|---|---|---|---|
| 토 14:50 | ryan | (v2) | v1 위에 실행 확인 추가: 짧은 후보를 컨테이너 안에서 patch된 트리에 대고 돌려서 통과한 건 뺌. fixture만 바뀐 커밋은 확인 안 함 | sample 158.93 s + 확인 CPU 2.3 s, 0 miss | | 확인을 전부 하면 CPU 16 s로 오히려 손해였음 |
| 토 12:55 | ryan | (v1) | Blast Radius v1: 테스트별 coverage 맵(fork-per-test, spawn 워커 포함) + 패치 정적 분석 + LPT 패킹 | sample 165.40 s, 0 miss | | oracle 94.35; Docker make sample 통과 |
| 토 11:30 | ryan | - | Blast Radius 스타터 그대로 make sample | sample 1210.47 s, 0 miss | | 서버 baseline 1614.1 (연습셋 ~40커밋) |
| 토 10:40 | ryan | - | 문서 초기화, 브리프 저장 | - | - | 리더보드 스냅샷은 01-tracks.md |
