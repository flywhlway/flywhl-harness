"""敏感路径清单：文件工具的托管基线与 bash 命令分析共用这一份。

早期版本里两条通道各维护一份：bash 分析器的 ``SECRET_HINTS`` 覆盖 ``.kube``、``.netrc``、docker 登录凭证等，
read 工具的托管基线却只拒绝 ``.ssh``、``id_rsa``、``.aws/credentials``。凭证落在工作区里时（在家目录启动、
项目里带着 ``.netrc`` 或 ``.docker/config.json``），``cat .kube/config`` 被判为高危要确认，read 工具却按“工作区内只读”
直接放行；bypass 模式下工作区外的也照读；grep 不在基线里，递归搜索会把凭证文件逐行带回上下文。
清单分叉之后谁也不会发现，所以收敛到这里，并由 ``tests/test_security.py`` 检查两条通道的结论一致。

分两档：

- ``CREDENTIAL_GLOBS``：凭证的固定存放位置。文件工具（read / grep / write / edit）在 **任何模式** 下都拒绝，
  写也拒绝——往 ``~/.ssh/authorized_keys`` 追加一行就是持久化后门；
- ``SECRET_HINT_GLOBS``：可能含密钥、但也常有正当用途的文件（项目 ``.env``、``.npmrc``、shell 历史）。读写都要人确认。

glob 用 ``fnmatch`` 匹配完整路径，``*`` 可以跨目录；权限层同时拿原始路径、解析符号链接后的绝对路径、
相对工作区路径去匹配，所以 ``../../.kube/config`` 和指向它的软链接都会命中。
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

CREDENTIAL_GLOBS: tuple[str, ...] = (
    "*/.ssh/*", "*id_rsa*", "*id_ed25519*",                    # SSH 私钥与 known_hosts / authorized_keys
    "*/.aws/*", "*/.config/gcloud/*", "*/.azure/*",            # 云厂商凭证与 SSO 缓存令牌
    "*/.kube/*",                                               # kubeconfig：集群证书与令牌
    "*/.docker/config.json",                                   # 镜像仓库登录凭证
    "*/.netrc", "*/.git-credentials", "*/.config/gh/*",        # HTTP 基本认证、Git 与 GitHub CLI 令牌
    "*/.pypirc", "*/.pgpass", "*/.vault-token", "*/.gnupg/*",  # 发包令牌、数据库口令、Vault 令牌、GPG 私钥环
    "*/etc/shadow", "/proc/*/environ",                         # 系统口令哈希（macOS 上 /etc 解析为 /private/etc）、进程环境变量
)
SECRET_HINT_GLOBS: tuple[str, ...] = ("*.env", "*/.env.*", "*/.npmrc", "*_history")

# bash 命令只能按字符串找线索：上面两档的每个位置都要能在这里命中（测试逐项核对），
# 另外加上只在命令里出现的写法（钥匙串查询）和更宽的 credentials 字样——命令分析的结论只是“需要确认”，宽一点代价不大
SECRET_HINTS = re.compile(
    r"(\.ssh/|id_rsa|id_ed25519|\.aws/|\.config/gcloud/|\.azure/|\.kube/|\.netrc|/etc/shadow|\.env\b|credentials"
    r"|\.npmrc|\.pypirc|\.pgpass|\.git-credentials|\.docker/config\.json|\.gnupg|\.config/gh/|\.vault-token"
    r"|_history\b|/proc/[^/\s]+/environ|find-generic-password|find-internet-password)")


def sensitivity(path: str | Path) -> str | None:
    """路径落在哪一档：``"deny"`` / ``"ask"`` / ``None``。用于目录遍历时逐个文件判断（权限层只看得到遍历的根）。"""
    s = str(path)
    if any(fnmatch.fnmatchcase(s, g) for g in CREDENTIAL_GLOBS):
        return "deny"
    if any(fnmatch.fnmatchcase(s, g) for g in SECRET_HINT_GLOBS):
        return "ask"
    return None


__all__ = ["CREDENTIAL_GLOBS", "SECRET_HINT_GLOBS", "SECRET_HINTS", "sensitivity"]
