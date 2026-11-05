set -eu
python3 - <<'PY'
import json
import os
import urllib.request

# 回环地址不走代理
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
url = f"http://127.0.0.1:{os.environ['BENCH_PORT']}/items.json"
with opener.open(url, timeout=10) as response:
    items = json.load(response)["items"]
with open("total.txt", "w", encoding="utf-8") as f:
    f.write(f"{sum(item['price'] for item in items):.2f}\n")
PY
