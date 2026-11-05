#!/usr/bin/env bash
# 全历史密钥扫描：镜像克隆 GitHub 上的全部引用，在一次性容器里跑 gitleaks。只读，不改历史。
#
#   scripts/secret_scan.sh <输出目录>
#
# 输出目录要放在仓库之外。报告已脱敏，但带提交作者和邮箱，不进仓库。
# 退出码：0 没有命中，1 有命中，2 运行失败。
set -euo pipefail

image="zricethezav/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
out="${1:?用法：scripts/secret_scan.sh <输出目录>}"

mkdir -p "$out"
out="$(cd "$out" && pwd)"
git clone --quiet --mirror https://github.com/flywhlway/flywhl-harness.git "$out/mirror.git"

# gitleaks 的致命错误退出码也是 1，所以把“有命中”单独指定成 3
code=0
docker run --rm --network none -v "$out:/scan:ro" "$image" \
  git /scan/mirror.git --log-opts="--all" --redact --no-banner --exit-code 3 --report-format json --report-path - \
  > "$out/report.json" || code=$?
if [ "$code" -ne 0 ] && [ "$code" -ne 3 ]; then
  echo "gitleaks 运行失败，退出码 $code" >&2
  exit 2
fi

# 只输出规则、文件和提交，不输出 Secret、Match 和作者字段
python3 -I -c '
import json, sys
rows = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"命中 {len(rows)} 条")
for r in rows:
    print(r["RuleID"], r["File"], r["Commit"][:7])
' "$out/report.json"
[ "$code" -eq 0 ]
