# Blast Radius: 작업 순서

## 준비 (한 번)

```sh
# 스타터 킷: repo 밖에 풀고 sample만 링크
mkdir -p ~/dev/br-kit && cd ~/dev/br-kit
curl -sSLO https://www.joinbuilders.org/buildathon/tracks/blast-radius/files/blast-radius-sample.tar.gz
tar xzf blast-radius-sample.tar.gz
git clone https://github.com/tobymao/sqlglot.git && git -C sqlglot checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
cd ~/dev/buildathon && ln -s ~/dev/br-kit/sample sample

# 도구 이미지 (라벨링 환경과 같은 베이스와 패키지). 맥에서는 arm64로 빌드해서 빠르게
docker build -t br-env tools/env
```

## 돌려 보기

| 무엇 | 명령 | 걸리는 시간 |
|---|---|---|
| 제출 이미지 그대로 (amd64, 채점 옵션) | `make sample` | 1분 안팎 |
| 이미지 빌드 없이 작업 트리로 | `tools/dev_sample.sh` | 30초 |
| 호스트에서 정적 선택만 + 종류별 비용 | `BLAST_SQLGLOT=~/dev/br-kit/sqlglot python3 tools/eval.py sample --verbose` | 몇 초 |

`dev_sample.sh`는 `BLAST_VERIFY=0` 같은 환경 변수를 `DOCKER_ENV="-e BLAST_VERIFY=0"`로 넘길 수 있다.

## 검증용 커밋

sample 30개만 보고 맞추면 과적합된다. 그래서 일부러 고친 커밋을 만들고 실제 테스트를 다 돌려서 정답을 만든다.

```sh
python3 tools/mutate/gen.py ~/dev/br-kit/sqlglot ~/dev/br-val/p --n 90 --seed 7
SHARDS=3 WORKERS=3 tools/mutate/label.sh ~/dev/br-kit/sqlglot ~/dev/br-val/p ~/dev/br-val/lab ~/dev/br-kit/sample/tests.json
```

라벨러는 공식 sample 정답을 그대로 재현하는지로 먼저 확인했다. 커밋 하나에 전체 테스트를 돌리니 80초쯤 걸린다. 데이터는 repo에 넣지 않는다.

## coverage 맵 다시 만들기

```sh
tools/covmap/run_coverage.sh ~/dev/br-kit/sqlglot /tmp/cov br-env
python3 tools/covmap/build_map.py /tmp/cov ... maps/coverage.json
```

테스트마다 fork해서 추적하고, 테스트가 띄우는 spawn 풀 워커도 `sitecustomize.py`로 같이 추적한다. 맵은 고정 커밋에서만 만든다 (규칙상 허용되는 coverage/import/fixture 맵).

## 제출

1. `make sample`로 miss 0 확인, `docs/log.md`에 로컬 점수 기록.
2. main에 push. 제출한 커밋은 결과 발표 전까지 지우거나 history를 바꾸면 안 된다.
3. 사이트에서 repo URL과 커밋 해시 입력. 시간당 1회.
4. 서버 점수(3회 중 중간값, CPU 포함)를 `docs/log.md`에 기록.
