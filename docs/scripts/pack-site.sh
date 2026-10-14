#!/usr/bin/env bash
# 一键构建文档站并把静态产物打成压缩包，附带 sha256 校验文件。
#
# 用法：
#   npm run pack:site                       # 构建 + 打包为 .tar.gz
#   npm run pack:site -- --zip              # 打包为 .zip
#   npm run pack:site -- --skip-build       # 跳过构建，直接打包现有 .vitepress/dist
#   npm run pack:site -- -o /tmp/out        # 指定输出目录（默认 docs/release）
#   BASE=/flywhl-harness/ npm run pack:site # 部署在子路径时，BASE 会透传给构建
set -euo pipefail

usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; }

format=tgz
skip_build=0
out_dir=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --zip) format=zip ;;
    --skip-build) skip_build=1 ;;
    -o | --out-dir)
      [[ $# -ge 2 ]] || { echo "错误：${1} 需要一个目录参数" >&2; exit 2; }
      out_dir="$2"; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "错误：未知参数 ${1}" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# 以 docs/ 为工作目录，脚本从任何位置调用都一样
cd "$(dirname "$0")/.."
docs_dir="$PWD"
dist_dir="$docs_dir/.vitepress/dist"
out_dir="${out_dir:-$docs_dir/release}"

if [[ $skip_build -eq 0 ]]; then
  [[ -d node_modules ]] || { echo "==> 未检测到 node_modules，执行 npm ci"; npm ci --no-audit --no-fund; }
  echo "==> 构建文档站（BASE=${BASE:-/}）"
  npm run build
fi

[[ -f "${dist_dir}/index.html" ]] || { echo "错误：${dist_dir}/index.html 不存在，请先构建" >&2; exit 1; }

# 包名：<包名>-v<版本>-<commit>[-dirty]，便于追溯产物对应的源码
version="$(node -p "require('./package.json').version")"
sha="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
if [[ "$sha" != nogit && -n "$(git status --porcelain -- . 2>/dev/null)" ]]; then sha="$sha-dirty"; fi
name="flywhl-harness-docs-v$version-$sha"

# 先拷到临时目录，让压缩包里有一层同名顶层目录，解压不会散落一地
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
cp -R "$dist_dir" "$stage/$name"
find "$stage/$name" -name .DS_Store -delete

mkdir -p "$out_dir"
out_dir="$(cd "$out_dir" && pwd)"
if [[ $format == zip ]]; then
  archive="$name.zip"
  rm -f "$out_dir/$archive"
  (cd "$stage" && zip -qrX "$out_dir/$archive" "$name")
else
  archive="$name.tar.gz"
  # COPYFILE_DISABLE 与 --no-xattrs：避免 macOS 打进 ._* 文件和扩展属性，Linux 解压时报警告
  (cd "$stage" && COPYFILE_DISABLE=1 tar --no-xattrs -czf "$out_dir/$archive" "$name")
fi

# 生成与 `shasum -a 256 -c` / `sha256sum -c` 兼容的校验文件
(
  cd "$out_dir"
  if command -v sha256sum >/dev/null; then sha256sum "$archive"; else shasum -a 256 "$archive"; fi >"$archive.sha256"
)

files="$(find "$stage/$name" -type f | wc -l | tr -d ' ')"
# 按实际字节数换算；du 统计的是磁盘占用，文件被覆盖写过后会偏大
size="$(wc -c <"$out_dir/$archive" | awk '{ s = $1; split("B KB MB GB", u); i = 1; while (s >= 1024 && i < 4) { s /= 1024; i++ } printf(i == 1 ? "%d %s" : "%.1f %s", s, u[i]) }')"
echo "==> 打包完成"
echo "    压缩包：${out_dir}/${archive}（${size}，${files} 个文件）"
echo "    校验：  ${out_dir}/${archive}.sha256"
if [[ $skip_build -eq 0 ]]; then
  echo "    BASE：  ${BASE:-/}（须与实际部署路径一致）"
else
  echo "    BASE：  沿用现有产物构建时的设置（已跳过构建）"
fi
