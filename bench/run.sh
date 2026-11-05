#!/usr/bin/env bash
# 在一次性容器里跑基准。不挂宿主目录，原始数据不出容器，取走的只有脱敏后的 /results/public。
#
#   bench/run.sh --env-file <密钥文件> --out <本机结果目录> [--spec 'flywhl-harness==0.2.0'] [子命令]
#
# 子命令默认 all（Q1 → Q2 → Q3 → 脱敏），也可以是 pilot、rehearse、validate。同一个提交、同一个子命令再跑一次会接着上次的容器继续。
# 密钥文件是 docker 的 --env-file 格式，只放这次新建、设了额度的专用密钥。
set -euo pipefail

env_file="" out="" spec="" command="all"
while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) env_file="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    --spec) spec="$2"; shift 2 ;;
    *) command="$1"; shift ;;
  esac
done
[ -n "$out" ] || { echo "要给 --out" >&2; exit 2; }
[ -z "$(git status --porcelain)" ] || { echo "有未提交的改动：结果要绑定到提交，先提交" >&2; exit 2; }
# 镜像只打包提交了的文件。这里用的是真实的全局忽略规则，测试的沙箱里读不到它
ignored=$(git ls-files -o -i --exclude-standard bench/tasks | grep -v __pycache__ || true)
[ -z "$ignored" ] || { printf '这些任务文件被 git 忽略，不会进镜像：\n%s\n' "$ignored" >&2; exit 2; }

commit=$(git rev-parse HEAD)
tag="flywhl-bench:${commit:0:12}"
name="flywhl-bench-${commit:0:12}-$command"
envs=()
[ -z "$env_file" ] || envs=(--env-file "$env_file")

git archive --format=tar.gz HEAD | docker build -q -t "$tag" -f bench/Dockerfile \
  --build-arg "BENCH_COMMIT=$commit" --build-arg "BENCH_SPEC=$spec" - >/dev/null

# 和本机别的容器隔开。本机网络多时默认地址池会用完，这时用固定网段
docker network inspect flywhl-bench >/dev/null 2>&1 || docker network create flywhl-bench >/dev/null 2>&1 \
  || docker network create --subnet "${BENCH_SUBNET:-10.250.87.0/24}" flywhl-bench >/dev/null

code=0
if docker container inspect "$name" >/dev/null 2>&1; then
  docker start -a "$name" || code=$?
else
  # bubblewrap 要建 user namespace、挂新的 /proc，默认的 seccomp、apparmor 和 /proc 遮蔽会拦。这几项是放宽，所以同时去掉全部 capability
  # --init：Python 当 PID 1 不回收退出的 bwrap、git 进程，僵尸会占满 --pids-limit，后面的试验 fork 失败
  docker run --init --name "$name" --network flywhl-bench --memory 4g --pids-limit 1024 \
    --cap-drop ALL --security-opt no-new-privileges \
    --security-opt seccomp=unconfined --security-opt apparmor=unconfined --security-opt systempaths=unconfined \
    ${envs[@]+"${envs[@]}"} "$tag" "$command" || code=$?
fi

mkdir -p "$out"
if docker cp "$name:/results/public/." "$out/" 2>/dev/null; then
  echo "结果在 $out"
else
  echo "这次没有产出可取走的结果（子命令 $command 只写容器里的 raw，或者中途停了）" >&2
fi
if [ "$code" -eq 0 ]; then
  docker rm "$name" >/dev/null
else
  echo "退出码 $code，容器 $name 留着，补足预算或修好问题后同样的命令接着跑" >&2
fi
exit "$code"
