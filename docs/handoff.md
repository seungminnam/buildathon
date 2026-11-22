# 인수인계 (작업 현황)

작업을 시작하기 전에 이 파일부터 읽고, 끝나면 맨 위 "현황"과 "작업 기록"을 고친다. 실험 숫자는 `log.md`, 결정은 `decisions.md`, 막혔던 것은 `br-issues.md`에 쓴다. 이 파일은 지금 누가 무엇을 하고 있는지만 짧게 적는다.

## 현황 (일 11/22 10:10 EST)

- 마감: 일 11:00 AM EST. 제출은 시간당 1회, 마지막 기회는 10:29.
- 파이널은 처음 보는 커밋으로 "고른 커밋" 하나만 채점한다. 고르지 않으면 연습 최고 점수 커밋이 나간다. miss 하나가 300 s라서 연습 점수보다 miss 0이 우선이다.
- 연습 최고: 140.3 s (2d9e2b4, 시뮬 126.8 + CPU 13.5). 5c57ca8은 140.9 s(같은 시뮬, CPU +0.6).
- main = 5c57ca8 이후 두 갈래를 합친 것:
  - ryan: pickle 왕복(직렬화 변경 놓침), import 안전망(모듈 수준 변경은 항상 import 에러 확인, 실패 모듈이 많으면 전체), 모듈 이름 변경 시 사용자 유지
  - SiHeonOh (`qa/robustness`, PR #1): 코딩 선언/비 UTF-8, 옮긴 모듈, pytest 설정, 멈춘 확인 10 s에서 중단, 다이제스트 20 s 상한, 기본 테이블 편집 좁히기
- 합친 버전 검증: sample 30, 검증 117, 새 검증 86, 직접 만든 특이 변경 7개 모두 miss 0 (같은 새 검증 세트에서 5c57ca8은 miss 2).
- 파이널로 고를 커밋: 10:29 제출하는 합친 버전. 연습 점수가 140.3보다 조금 나빠도 "Pick as our final entry"로 직접 고른다.

## 누가 무엇을

| 사람 | 하는 일 | 브랜치 |
|---|---|---|
| ryan | 제출 관리, 검증 세트 라벨링(`~/dev/br-val`), 통합 | main |
| SiHeonOh | 새 변경 종류 QA, 견고성 | qa/robustness |

## 검증하는 법

```sh
# 라벨: 커밋마다 전체 테스트를 돌려 정답을 만든다 (커밋당 약 2분, 메모리 2.5 GB면 충분)
docker run -d --rm --cpus 2 --memory 2500m --user 65534:65534 -e HOME=/tmp -e PYTHONHASHSEED=0 \
  --tmpfs /tmp:rw,size=256m --read-only -v <sqlglot>:/opt/sqlglot:ro -v $PWD:/app:ro \
  -v <patch dir>:/p:ro -v <out dir>:/out -v <tests.json>:/data/tests.json:ro \
  br-env python -u /app/tools/label_fast.py /p /out/l0.jsonl 0/1

# 세트로 모으기, 이미지로 돌리기, 채점
python3 tools/val_set.py <patch dir> <label dir> <kinds.json> <tests.json> <set dir>
docker build -t br-eval . && docker run ... br-eval > out.jsonl   # 채점기 옵션은 Makefile 참고
python3 score.py <set dir> <set dir>/answers.json out.jsonl
```

- `tools/label_fast.py`는 공식 sample 정답과 똑같이 나오는지 확인한 뒤에 쓴다. 러너를 바꾸면 다시 확인한다(br-issues 22).
- 빌드할 때 테스트 실행 시간을 재므로(`blast/bench.py`), 라벨링과 동시에 빌드하면 시간이 부풀어서 판단이 보수적으로 나온다. 점수 비교는 조용할 때 빌드한 이미지로 한다.

## 작업 기록

최신이 위.

- 일 10:15 ryan: 새 검증 86개 라벨 완료, 합친 버전 miss 0. 10:29에 제출하고 파이널로 고르기로 함.
- 일 10:10 ryan: PR #1을 main에 합침. 충돌 두 곳은 양쪽 보호를 모두 살림(모듈 이름 변경은 import하는 테스트 + 사용자, import 실패 처리는 3개 초과면 전체).
- 일 09:50 ryan: 직접 만든 특이 변경 테스트. dialect 모듈 이름 변경이 후보 0개였던 것 수정.
- 일 09:45 ryan: 새 검증 커밋 29개에서 miss 2개(import가 깨지는 변경). import 안전망 추가.
- 일 09:30 ryan: 인라인 풀이 pickle을 건너뛰어 직렬화 변경을 놓치던 버그 수정, 5c57ca8 제출(140.9).
- 일 09:11 SiHeonOh: `qa/robustness` 브랜치, 새 변경 종류 13개와 테이블 편집 59개로 QA.
