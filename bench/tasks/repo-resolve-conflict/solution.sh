set -eu
# 这里预期会冲突；没有冲突的话后面的 commit 会失败，校验也就不过了
git merge --no-edit feature || true
cat > settings.py <<'PY'
"""服务配置。"""

ALLOWED_ORIGINS = [
    "https://admin.example.com",
    "https://app.example.com",
    "https://partner.example.com",
]

TIMEOUT_SECONDS = 60
RETRIES = 3
LOG_LEVEL = "info"
PY
git add settings.py
git commit -q --no-edit
