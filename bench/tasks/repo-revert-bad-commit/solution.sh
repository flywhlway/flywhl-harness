set -eu
bad=$(git log --format=%H --grep='^refactor: 整理折扣计算的取整写法$')
git revert --no-edit "$bad"
