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

## 10. 한 줄 lambda가 coverage 맵에 안 잡힌다

검증 커밋 av990007에서 miss가 났다. `exp.CurrentTimestamp: lambda *_: "CURRENT_TIMESTAMP"`의 문자열만 바꾼 변경인데, 맵에는 그 줄을 실행한 테스트가 0개였다. 실제로는 7개가 깨진다.

원인은 `sys.monitoring`이다. LINE 이벤트만 켜면, 몸통이 시작 줄과 같은 줄에 있는 함수(한 줄 lambda)는 그 줄에 대해 LINE 이벤트가 안 온다. 작은 스크립트로 재현했다. sqlglot generator의 `TRANSFORMS`에는 이런 lambda가 수백 개 있어서 위험이 크다. 추적할 때 PY_START(함수 시작) 이벤트도 켜고, 시작한 함수의 첫 줄을 실행한 줄로 센다.

## 11. Expression 클래스를 누가 쓰는지 모른다

검증 커밋 av990015에서 miss가 났다. `YearOfWeek._sql_names`에서 이름 하나를 바꿨는데 `test_annotate_funcs`가 빠졌다. 맵은 Dialect/Tokenizer/Parser/Generator만 "누가 만들었는지" 기록하고 Expression 클래스는 기록하지 않았다. 또 이 테스트는 실제 작업을 프로세스 풀에서 하는데, 하위 프로세스에서는 어떤 클래스를 만들었는지 아예 기록하지 않았다. `Expr`도 기록 대상에 넣고, 하위 프로세스에서는 모듈이 import되는 순간 `__init__`을 감싸도록 `sitecustomize.py`에 import 훅을 넣었다. 테스트 하나로 확인한 뒤 맵을 다시 만들었다.

## 12. import 때 만들어지는 클래스는 "쓰는 테스트"로 좁히면 안 된다

검증 커밋 bv070000에서 miss가 났다. `Select.arg_types`에서 `from_`을 필수로 바꾸니 1020개 테스트가 깨졌다. sqlglot과 테스트 모듈이 import될 때 이미 `Select`를 만들어서 import 자체가 실패하기 때문이다. 맵에 "import 중에 만들어진 클래스"를 따로 저장하고, 그런 클래스의 클래스 수준 정의가 바뀌면 전체 테스트를 후보로 둔다.

## 13. 짧은 테스트가 엉뚱한 이유로 긴 테스트를 대신 판단했다

검증 커밋 bv070013에서 v3가 miss를 냈다. 이 커밋은 tsql 키워드 하나와 `tpc-ds.sql` fixture를 같이 바꿨다. 깨지는 건 fixture를 읽는 `test_tpcds`(2.2 s, 긴 테스트) 하나뿐이다. 그런데 키워드 변경 때문에 후보가 된 짧은 테스트 170개가 다 통과하자 긴 테스트까지 뺐다. 이제 파일마다 따로 분석한다. 긴 테스트를 빼려면 그 테스트를 후보로 만든 모든 파일이 파이썬 코드여야 하고, 같은 파일 때문에 후보가 된 짧은 테스트가 3개 이상 있어야 한다.

## 14. "전체 테스트" 폴백 줄이기: import probe

import 중에 도는 함수(파서 테이블을 만드는 함수 등)가 바뀌거나 import 때 만들어지는 클래스가 바뀌면 지금까지는 전체 테스트(36.8 s)로 물러났다. 이제는 자식 프로세스에서 patch된 sqlglot과 테스트 모듈을 전부 import한다. 에러가 나면 전체를 그대로 두고, 성공하면 클래스 속성과 모듈 전역값의 지문을 원본과 비교한다. 바뀐 게 없으면 import 쪽 영향은 없다고 보고 실행 시점 후보만 남긴다. 한 번에 CPU 약 0.75 s다.

같은 트리를 두 번 import해도 지문이 다른 값이 있었다. 메모리 주소가 들어간 repr, set에서 만든 tuple의 순서, abc 내부 캐시 같은 것들이다. 원본을 두 번 probe해서 서로 다른 키는 잡음으로 빼고, `functools.partial`은 따로 풀어서 비교한다.

## 15. tpcds를 돌리지 않고 결과가 같은지 알아내기

검증 세트 낭비의 대부분이 `test_execute_tpcds`(21.5 s)였다. 실제로는 안 깨지는데 바뀐 줄을 지난다는 이유로 들어간 경우다. 이 테스트가 patch에 영향을 받는 길은 쿼리마다 정해진 출력 몇 개뿐이다. 파싱 결과, pickle 왕복, DuckDB로 바꾼 SQL, 최적화된 SQL, 플랜, executor가 만드는 파이썬 코드, 그리고 그 코드를 실행한 결과다. 이걸 쿼리별로 해시해서 원본과 비교한다(`blast/tpcdigest.py`). 실행은 테이블마다 앞 30줄로만 한다. 전부 같으면 테스트에 들어가는 입력과 실행되는 코드가 원본과 같으니 결과도 같다. executor 자체(`sqlglot/executor/`)나 TPC 데이터 파일이 바뀌면 이 논리가 성립하지 않아서 쓰지 않는다.

처음엔 같은 트리를 두 번 돌려도 값이 달랐다. 원인은 둘이었다.
- 문자열 해시가 프로세스마다 달라서(`PYTHONHASHSEED`) 최적화 결과의 순서가 바뀌었다. 이미지에서 `PYTHONHASHSEED=0`으로 고정했다.
- 플랜을 문자열로 바꾸면 객체 id와 의존 순서가 섞여 나왔다. 의존 관계를 정렬해서 직렬화했다.

원본 값은 빌드할 때 만든다. 빌드 시간은 점수에 안 들어간다. 다르면 첫 쿼리에서 바로 멈추고(0.3 s 안팎), 같으면 전체를 도는 데 CPU 5~8 s가 든다. 검증 커밋에서 tpcds가 실제로 깨지는 커밋은 전부 "다름", 안 깨지는 커밋은 하나만 빼고 전부 "같음"으로 나왔다.

## 16. import 에러가 나도 전부 깨지지는 않는다

함수 이름을 바꾸거나 import를 지우면 그 모듈의 import가 실패한다. 지금까지는 "수집할 때 import되는 모듈이니 모두가 쓴다"며 전체 테스트를 넣었다. 하지만 dialect 하나의 generator가 import에 실패해도 그 dialect를 안 쓰는 테스트는 멀쩡하다. probe가 어떤 모듈이 실패하는지 알려주므로, 그 모듈의 코드를 실행했거나 그 파일을 열었거나 그 모듈의 클래스를 만든 테스트만 넣는다. sqlglot 자체나 테스트 패키지가 import에 실패하면 그때는 전체다. 검증 커밋 av990021이 36.8 s에서 8.8 s(정답과 같음)로, bv070052가 36.8 s에서 10.7 s로 줄었다.

## 17. 클래스 테이블의 한 항목만 바뀐 경우

`FUNCTIONS`, `KEYWORDS`, `TRANSFORMS` 같은 표에서 한 줄만 바뀌면 그 클래스를 쓰는 테스트 수백 개가 후보가 됐다. 이제는 바뀐 항목의 키로 좁힌다. `exp.Foo`가 키면 `exp.Foo`를 실제로 만든 테스트(맵에 기록), SQL 문자열이 키면 테스트 클래스 소스나 읽은 fixture에 그 문자열이 나오는 테스트만 남긴다.

처음엔 `arg_types`(인자 이름이 키)에도 문자열 검색을 적용했다가 miss가 났다(av990018, `'kind'`). `arg_types`는 표현식을 만들고 검증하는 방식 자체를 바꾸니 키 문자열과는 관계가 없다. 문자열 키 방식은 `FUNCTIONS`, `KEYWORDS`, `*_PARSERS`, `*_MAPPING` 표에만 쓴다.

## 18. v6(7f3e94a) 서버 점수 역전: 시뮬레이션은 줄었는데 CPU가 두 배 가까이 뛰었다

7f3e94a 제출 결과 시뮬레이션은 172.4 s → 151.6 s(-20.8 s, miss 0)로 줄었지만, CPU가 30.7 s → 56.7 s(+26.0 s)로 늘면서 총점이 203.1 s → 208.3 s로 나빠졌다. 프로파일링해 보니 원인은 네 군데였다.

- **pytest 기동 오버헤드:** sqlglot 테스트 1,620개 전부가 표준 `unittest.TestCase`인데도 커밋마다 `pytest.main()`을 띄워 0.7 s씩 쓰고 있었다. 직접 `unittest` 러너(`blast/verify.py`)로 바꾸고, `ProcessPoolExecutor`를 인라인 실행으로 바꾸고, TPC 테스트가 없을 때는 `TestExecutor.setUpClass`의 TPC CSV 로딩을 건너뛰자 fork+import 고정비가 700 ms → 32 ms로 줄었다.
- **실제 실행시간과 기록 시간의 괴리:** `tests.json` 기록 시간은 pytest/xdist/spawn 기준이라 실제 직접 실행 시간과 3~85배까지 차이 난다(`test_generator_memory` 6.69 s vs 실제 0.5 s, `test_agg_order` 1.36 s vs 실제 3 ms). 빌드 시점에 `blast/bench.py`로 채점기와 같은 CPU에서 1,616개 테스트의 실제 CPU 시간을 측정해 `maps/actual_ms.json`으로 저장하고, 검증 여부와 예산을 실제 시간 기준으로 결정하도록 바꿨다.
- **TPC 다이제스트 병목:** `_plan_key`가 재귀적 `str(step)`을 호출해 $O(2^N)$으로 2.2 s를 태우고 있었고, 실행하지 않는 TPC-DS 쿼리 42개까지 `optimize`/`Plan`/`PythonExecutor`를 돌리고 있었다. `step._to_s("")` 기반 $O(N)$ 직렬화로 바꾸고, 실행 쿼리(57개)만 실행 단계를 돌며, 비실행 쿼리는 `parse_one`+`pickle` 성공 여부만 확인하도록 고쳤다(검증 커밋 bv070013에서 21.5 s 절감). 추가로 빌드 때 쿼리별 라인 커버리지와 `duck`/`py` 단계별 파일을 기록해 바뀐 코드가 닿는 쿼리와 단계만 다이제스트한다.
- **fork 후 `gc.collect()` 폭증과 nanobind 경고:** 부모 프로세스에 올라간 4.2 MB 커버리지 맵이 fork된 자식에게 그대로 상속돼, `test_generator_memory`처럼 `gc.collect()`를 수십 번 부르는 테스트가 매번 맵 전체를 훑느라 0.5 s짜리가 6.18 s로 느려졌다. 정적 맵을 올린 직후(`duckdb`를 import하기 전) `gc.freeze()`를 호출해 해결했다. `duckdb` import 이후에 `gc.freeze()`를 부르면 nanobind 종료 시 refcount 경고가 떠서 반드시 그 전에만 동결한다.

## 19. probe 잡음과 import 오탐

- `probe.py`의 기준 지문을 런타임에 두 번 재던 것을 빌드 시점(`maps/probe_base.json`)으로 옮겨 런타임 CPU를 약 1.5 s 아꼈다.
- `sqlglot/expressions/core.py`의 `UNITTEST = "unittest" in sys.modules` 값이 빌드 때와 런타임에 달라서 잡음으로 잡히던 문제를 `_NOISE` 패턴에 추가해 고쳤다.
- 최상위 함수의 기본 인자만 바뀐 경우(`bv070082`, `simplify`)는 클로저가 아니므로 `RULES` 리스트 지문 비교에서 제외해 전체 폴백을 막았다.

## 20. PROXY가 일반 테스트까지 대신 빼다가 miss가 났다

`bv070082`에서 `PROXY` 로직이 일반 장시간 테스트(`test_tpcds`, 2.2 s)까지 "짧은 후보들이 다 통과했으니 빼자"며 생략해 miss가 났다. `test_tpcds`만 깨지는 변경이었기 때문이다. `VOUCHABLE`은 진짜 거대 테스트(`GIANT_TESTS`: `test_max_nodes`, `test_execute_tpcds`, `test_execute_tpch`, `test_optimized_tpch`)로만 제한하고, TPC 다이제스트를 쓸 수 있는 커밋에서는 TPC 테스트를 PROXY로 빼지 않도록 막았다. 대신 거대 테스트가 아닌 후보 하나만 남았을 때는 실제 실행 시간이 짧으면(`<= 2.3 s`) 한 번 더 직접 돌려 확인한다.

## 21. 긴 단위 테스트의 부분 실행 (per-item sub-coverage)

검증 CPU의 대부분은 100~200개 후보 테스트 전체를 돌리면서, 그 안의 수백~수천 개 SQL 케이스 중 바뀐 줄을 지나지 않는 케이스까지 전부 실행하는 데서 나왔다. 빌드 시점(`blast/subcov.py`, 4워커 fork, 약 23 s)에 229개 테스트의 세부 항목별 커버리지를 수집해 `maps/subcov.json`에 저장한다.

- **SQL fixture / dialect 루프 (30개):** fixture 항목별·dialect별 커버리지.
- **서브테스트 (`__subtests__`, 17개):** `with self.subTest(...)` 블록을 AST로 감싸 실행 항목별 커버리지 기록.
- **독립 문장 블록 (`__blocks__`, 5개):** `test_scope`처럼 `subTest` 없이 긴 테스트는 def-use 분석으로 독립 문장 컴포넌트를 나누고 항목별 시간 가중치(`w`)까지 기록.
- **dialect validator (`__vcalls__`, 177개):** `validate_identity`/`validate_all` 호출 줄별 커버리지.

`Dialect.get_or_raise()`가 인스턴스를 캐시하기 때문에, `Tokenizer`/`Parser`/`Generator`의 `__init__`에서 `type(self.dialect)`도 같이 기록해야 캐시 이후 항목에서도 해당 `Dialect` 클래스가 누락되지 않는다.

## 22. slack-aware 2패스 검증 게이트와 프로세스 전역 캐시 가드

- **2패스 게이트:** 한 커밋에서 이미 실패한 테스트나 `dear` 테스트가 생기면, 이후 남은 테스트 하나를 통과시켜 아끼는 시뮬레이션 시간이 실제 실행 CPU보다 작아지는 경우가 많다. 빠른 테스트(`<50 ms`)를 먼저 돌려 실패 여부를 일찍 확인하고, 남은 느린 테스트는 한계 절감액(`p_pass * delta`)이 예상 CPU(`cpu_ratio * est`)를 넘지 않으면 건너뛰도록 했다(`blast/verify.py`).
- **전역 지연 캐시 가드:** `generator.py`의 `_build_dispatch`(`_DISPATCH_CACHE`)와 `dialect.py`의 `Dialect._try_load`는 프로세스당 한 번만 실행돼 단일 프로세스 커버리지에 첫 테스트만 기록된다. 이 구간이 수정되면 정적 분석(`blast/impact.py`)에서 전체 폴백하고 `subcov` 필터도 비활성화한다.
- **`probe.fingerprint()` 메모이제이션:** `_canon()`에 객체 `id` 단위 캐시를 넣어 중복 순회를 없애고, `probe` 실행 전 `setuptools`/`doctest`를 부모에서 미리 워밍업해 probe CPU를 줄였다.

## 23. amd64 임계치 보정과 LazyFuture 조기 종료

v8(`c8cd459`, 서버 **140.7 s** = 시뮬 126.75 + CPU 13.96, miss 0)을 amd64 이미지로 `val` 117개에 돌려보니 arm64 대비 시뮬레이션 초과가 컸다. 원인은 세 가지였다.

- **절대값 임계치:** `cheap`·`batch_cap`·`subset_mode` 임계치가 arm64 기준 고정 ms여서, CPU가 느린 환경에서는 `ACTUAL_MS`가 올라가며 배치 전체가 `listed`로 빠지거나 `subset_mode`로 잘렸다. 기록 시간(`ms[n]`, `listed`) 대비 무차원 비율로 바꿔 CPU 속도에 맞게 자동 조정되게 했다.
- **`budget_ctx` 과다 스킵:** 첫 실패가 나기 전에도 스킵하거나 누적 스킵 시간이 커져 워커 비용이 늘어나는 경우가 있었다. 첫 실패 전에는 스킵하지 않고, 이후에도 0 ms 증가 또는 소폭 증가(`<= 250 ms`, 누적 `<= 500 ms`)만 허용하도록 고쳤다.
- **실패 테스트의 선제 실행:** `TestOptimizer.check_file`은 `pool.submit()`으로 99~300개 쿼리를 전부 제출한 뒤 `wait()`·`as_completed()`로 검사한다. 기존 `InlineExecutor.submit()`은 제출 시점에 즉시 실행해서 1번 쿼리가 실패해도 나머지 299개를 다 돌고 있었다. `_LazyFuture`로 `.result()` 호출 시점에 지연 실행하고 `as_completed`/`wait`를 패치해 첫 실패 쿼리에서 바로 빠져나오게 바꾸자 `val` 검증 CPU가 3.5 s 줄었다.

## 24. executor 코드에 30행 미니 테이블 쿼리 필터를 쓰면 안 된다

`sqlglot/executor/*` 변경에도 TPC 다이제스트의 쿼리별 라인 커버리지(`q_lines`, 30행 미니 테이블 기준)를 적용해 보려다, `sqlglot/executor/env.py`의 `sql_not`을 깨는 테스트 케이스에서 miss가 나는 것을 확인했다. 30행 미니 테이블에서는 `sql_not`을 지나는 쿼리가 1개뿐이지만, 전체 CSV 테이블에서는 다른 쿼리도 해당 분기를 타기 때문이다. 따라서 `sqlglot/executor/` 변경은 기존대로 `tpc_applicable=False`를 유지하고 전체 테이블 커버리지(`coverage.json`)만 사용한다. 반면 파싱·최적화·플랜·코드 생성·DuckDB transpilation은 행 수와 무관하므로 비-executor 파일에 대한 `q_lines`와 `py_core_lines`는 안전하다.

## 25. 딕셔너리 중복 키와 `test_no_redundant_expression_metadata`

클래스/모듈 딕셔너리 항목 차분(`blast/impact.py`의 `dict_entry_change`)을 다중 줄·추가·삭제·`**DictComp`·모듈 수준 `EXPRESSION_METADATA`까지 확장하면서 두 가지 함정을 잡았다.

- **중복 키:** `PostgresGenerator.TRANSFORMS`처럼 원본 sqlglot 코드에 같은 키(`exp.UnixToTime`)가 중복 선언된 딕셔너리가 있다. 키당 단일 값이 아니라 값 리스트로 그룹화해 비교해야 중복 키가 있는 테이블에서도 정상적으로 좁혀진다.
- **메타데이터 전수 검사:** `tests/test_typing.py`의 `test_no_redundant_expression_metadata`는 `exp.*` 인스턴스를 만들지 않고 `sqlglot.typing.*.EXPRESSION_METADATA` 딕셔너리 전체를 순회한다. `EXPRESSION_METADATA` 변경 시에는 항상 `tests/test_typing.py`와 해당 파일을 연 테스트를 합집합으로 포함시켜 누락을 막았다.



## 22. 인라인 풀이 직렬화 버그를 숨겼다

검증용 라벨러를 다시 돌리다가 sample 3utz(`serde.py` 변경) 라벨이 공식 정답과 달랐다. 깨져야 할 optimizer/executor 테스트 24개가 통과로 나왔다. 실제 테스트는 `ProcessPoolExecutor`로 일을 넘길 때 식 트리를 pickle해서 보내고 결과를 pickle해서 받는다. 직렬화 코드가 바뀌면 바로 그 지점에서 깨진다. 그런데 CPU를 아끼려고 넣은 인라인 실행기는 같은 프로세스에서 함수를 바로 불러서 pickle을 건너뛰었다.

같은 문제가 제출 코드의 확인 단계에도 있었다. 직렬화만 건드리는 변경이 오면 실제로는 깨지는 테스트를 "통과"로 보고 뺄 수 있다는 뜻이다. sample에서는 다른 테스트가 먼저 깨져서 운 좋게 걸리지 않았다. 인라인 실행기가 호출(함수, 인자)과 결과를 실제 풀처럼 pickle 왕복하도록 고쳤다. 고친 뒤 3utz 37개, 2jrq 8개 모두 공식 정답과 같다. 추가 CPU는 optimizer 테스트 164개 기준 2% 안팎이다.
