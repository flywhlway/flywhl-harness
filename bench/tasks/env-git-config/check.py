"""三项都要写在仓库本地配置里；origin 只能有一个地址，旧地址不能留着。"""

import subprocess
import sys

EXPECTED = {"user.name": "Bench Dev", "user.email": "dev@bench.example.invalid",
            "remote.origin.url": "https://git.bench.example.invalid/team/notes.git"}

for key, want in EXPECTED.items():
    got = subprocess.run(["git", "config", "--local", "--get-all", key], capture_output=True, text=True, timeout=10,
                         stdin=subprocess.DEVNULL).stdout.splitlines()
    assert got == [want], f"{key} 应当是 {want}，现在是 {got}"
