set -eu
cat > .git/hooks/pre-commit <<'HOOK'
#!/bin/sh
# 只看暂存改动里新增的行；-U0 去掉上下文，再跳过 +++ 文件头
added=$(git diff --cached --no-color --unified=0 | grep '^+' | grep -v '^+++ ')
case $added in
    *TODO*)
        echo "提交被拒绝：暂存的改动里新增了 TODO" >&2
        exit 1
        ;;
esac
HOOK
chmod +x .git/hooks/pre-commit
