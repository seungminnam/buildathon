# Blast Radius: 막혔던 것과 해결

시간순. 같은 문제를 다시 겪지 않으려고 적는다.

## 1. 테스트가 spawn 프로세스 풀을 쓴다

`tests/test_optimizer.py`와 `tests/test_executor.py`는 `ProcessPoolExecutor(mp_context=get_context("spawn"))`로 실제 작업을 하위 프로세스에 넘긴다. 여기서 세 가지 문제가 생겼다.

- **spawn된 프로세스가 우리 메인 스크립트를 다시 실행한다.** spawn은 `__main__` 모듈을 다시 읽는다. 디버그 스크립트에 `if __name__ == "__main__"` 가드가 없어서 자식마다 전체 작업이 다시 돌았고, CPU 300초에 엉뚱한 실패가 쏟아졌다. 진입점은 반드시 가드를 둔다. 제출용 `runner/main.py`도 마찬가지다.
- **CPU가 기록 시간보다 훨씬 많이 든다.** 풀 크기 기본값이 호스트 코어 수라서, `--cpus 2`로 제한해도 코어 수만큼 프로세스를 띄우고 각자 sqlglot, pandas, duckdb를 다시 import한다. 그래서 자식 안에서는 `get_context("spawn")`을 fork로 바꾸고 `os.process_cpu_count`를 2로 고정했다. 계산 결과는 같고 import 비용이 사라진다.
- **coverage 맵에 구멍이 생긴다.** 추적 플러그인은 메인 프로세스만 보기 때문에 풀 안에서 돈 optimizer 코드가 맵에 하나도 안 잡힌다. sample 30개 중 miss 4개(2jrq, 3utz, ary6, rszg)가 전부 이 경우였다. 추적할 때는 `ProcessPoolExecutor`를 같은 프로세스에서 바로 실행하는 대체 클래스로 바꿔서 다시 맵을 만든다 (`tools/covmap/trace_plugin.py`의 `_InlineExecutor`).

## 2. 처음 만든 BaseContext 패치가 무한 재귀

`multiprocessing.context.BaseContext.get_context`까지 바꿨더니, `DefaultContext.get_context`가 `super()`를 부르면서 다시 내 함수로 돌아와 재귀가 났다. 테스트 setup에서 전부 ERROR가 났다. 모듈 수준의 `multiprocessing.get_context`만 바꾸는 걸로 충분하다. 테스트 파일이 import할 때 이 이름을 가져가기 때문이다.

## 3. fork 자식이 출력을 버리고 죽는다

자식은 `os._exit(0)`로 끝나서 버퍼에 남은 stdout이 flush되지 않는다. 디버그 모드(`BR_DEBUG=1`)에서는 끝나기 전에 flush하도록 했다. 제출 모드에서는 자식의 stdout을 `/dev/null`로 보낸다. stdout에 답 외의 줄이 하나라도 섞이면 run 전체가 reject되기 때문이다.

## 4. 로컬 CPU 경쟁

같은 맥에서 다른 작업 브랜치의 라벨링 컨테이너가 pytest worker 12개를 돌리고 있어서 측정 시간이 크게 흔들린다. CPU 시간은 wall time보다 덜 흔들리지만 그래도 절대값은 믿지 않는다. 비교는 같은 조건에서 연달아 돌려서 한다.

## 5. 검증용 patch가 전부 "새 파일"로 읽혔다

`tools/mutate/gen.py`가 diff 헤더에 `index 0000000..1111111`을 썼다. GNU patch는 앞쪽 해시가 0이면 새 파일을 만드는 diff로 보고, 이미 있는 파일이라며 거절한다. 120개 중 119개 라벨이 에러였다. 새 파일이 아닌 diff는 0이 아닌 가짜 해시를 쓰도록 고치고 다시 라벨링했다. 라벨 파일에 `error`가 있는 줄은 평가에서 빼야 한다. 0개 실패로 세면 정답이 틀어진다.

## 6. 원격 main이 바뀌어 있었다

다른 작업 브랜치의 v1이 이미 원격 main에 올라가 있었다. 제출 전이라 history를 정리하면서 덮어썼고, v1 내용은 새 커밋에 전부 들어 있다. 제출한 커밋이 생기면 그때부터는 main을 다시 쓰지 않는다.

## 7. 첫 서버 점수: 266.0 s (시뮬레이션 229.4 + CPU 36.6)

연습 세트는 커밋 40개다 (baseline 1614.1 ÷ 커밋당 40.35). miss는 0이었지만 1등(121.8)과 두 배 차이가 났다. sample 기준으로 보면 손해 대부분은 바뀐 줄을 지나기만 하고 깨지지는 않는 tpcds(21 s), max_nodes(29 s) 같은 긴 테스트를 넣은 데서 나왔다. 1등은 커밋당 3.0 s라서 정답 하한(sample 기준 커밋당 3.15 s)과 거의 같다. 긴 테스트를 거의 넣지 않는다는 뜻이다.

CPU 36 s도 sample에서 잰 값(약 4 s)과 너무 다르다. 연습 세트에서 확인 실행이 훨씬 많이 돌았거나 시간 한도까지 붙잡은 확인이 있었던 것 같다. 서버는 run별 합계만 알려줘서 커밋별로는 알 수 없다.

## 8. 기존 라벨러를 믿을 수 없었다

pytest-xdist로 전체 테스트를 돌리는 라벨러가 커밋 하나에 3~10분 걸렸고, 컨테이너 셋 중 둘이 중간에 사라졌다. worker마다 TPC 테이블을 올리느라 메모리가 모자랐던 것으로 보인다. executor 테스트가 "flaky"로 찍혔고, pytest 내부 에러(exit 3)로 끝난 커밋이 "실패 0개"로 기록되기도 했다. 제출용 확인 코드(blast/verify.py)로 전체를 돌리는 `tools/label_fast.py`로 바꿨다. 커밋당 약 2분이고, 먼저 공식 sample 정답과 똑같이 나오는지 확인한다.

## 9. 확인이 실패를 계속 돌리느라 CPU를 태웠다

싼 후보로 긴 테스트를 대신 판단하게 바꿨더니, 3utz처럼 후보가 줄줄이 깨지는 커밋에서 실패 34개를 시간 한도까지 돌려 CPU 36 s를 썼다. 실패하는 테스트는 traceback과 pandas 비교 때문에 통과할 때보다 훨씬 느리다. 긴 후보가 있는 커밋은 어차피 긴 테스트를 넣어야 하니, 첫 실패가 보이면 바로 멈추고 아직 안 돈 짧은 후보는 그냥 넣도록 바꿨다. sample 확인 CPU가 68 s에서 15 s로 줄었고 시뮬레이션 초는 그대로다.
