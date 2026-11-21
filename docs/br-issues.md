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
