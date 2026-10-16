"""敏感路径清单：文件工具的托管基线与 bash 命令分析共用这一份。

早期版本里两条通道各维护一份：bash 分析器的 ``SECRET_HINTS`` 覆盖 ``.kube``、``.netrc``、docker 登录凭证等，
read 工具的托管基线却只拒绝 ``.ssh``、``id_rsa``、``.aws/credentials``。凭证落在工作区里时（在家目录启动、
项目里带着 ``.netrc`` 或 ``.docker/config.json``），``cat .kube/config`` 被判为高危要确认，read 工具却按“工作区内只读”
直接放行；bypass 模式下工作区外的也照读；grep 不在基线里，递归搜索会把凭证文件逐行带回上下文。
清单分叉之后谁也不会发现，所以收敛到这里，并由 ``tests/test_security.py`` 检查两条通道的结论一致。

分两档：

- ``CREDENTIAL_GLOBS``：凭证的固定存放位置。文件工具（read / grep / write / edit）在 **任何模式** 下都拒绝，
  写也拒绝——往 ``~/.ssh/authorized_keys`` 追加一行就是持久化后门；bash 命令命中对应的 ``CREDENTIAL_HINTS`` 同样拒绝；
- ``SECRET_HINT_GLOBS``：可能含密钥、但也常有正当用途的文件（项目 ``.env``、``.npmrc``、shell 历史）。读写都要人确认；
  bash 命中 ``SECRET_HINTS`` 判为高危，default 模式下要确认。

两档的取舍（为什么 bash 只对凭证档硬拒绝）写在 ``permissions.py`` 的模块注释里。``tools/sandbox.py`` 的 sandbox-exec 档位
也从 ``CREDENTIAL_GLOBS`` 生成内核层面的读写拒绝。

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

# bash 命令只能按字符串找线索，同样分两档，结论不同（见 permissions.py 模块注释“bash 与凭证位置”）：
#
# - CREDENTIAL_HINTS 对应 CREDENTIAL_GLOBS，命中即拒绝、任何模式都一样。硬拒绝没有“批准后执行”的出路，误报只能换个做法，
#   所以只收录除了碰凭证几乎不会出现在命令里的字样：目录按完整的路径段匹配（~/.ssh、~/.ssh/x 命中，ssh_utils.py、
#   .ssh_notes 不命中），再加上指向这些位置的环境变量（cat $KUBECONFIG）；
# - SECRET_HINTS 是两档的并集，再加上更宽的字样（.env、credentials、shell 历史、钥匙串查询）。只判为高危要确认——
#   cp .env.example .env、pytest tests/test_credentials.py 都会命中，宽一点的代价只是多问一次。
#
# 两档各自要覆盖对应 glob 的每个位置，测试逐项核对，也核对线索档的样例不会落进凭证档
_SEG_START, _SEG_END = r"(?<![\w.-])", r"(?![\w.-])"   # 前后不能紧挨着名字字符：整段匹配
CREDENTIAL_HINTS = re.compile(
    rf"{_SEG_START}\.(ssh|aws|azure|kube|gnupg){_SEG_END}|\.config/(gcloud|gh){_SEG_END}"
    r"|id_rsa|id_ed25519|\.docker/config\.json|\.netrc\b|\.git-credentials|\.pypirc|\.pgpass|\.vault-token"
    r"|/etc/shadow|/proc/[^/\s]+/environ"
    r"|\$\{?(KUBECONFIG|GNUPGHOME|DOCKER_CONFIG|CLOUDSDK_CONFIG|GH_CONFIG_DIR|AWS_SHARED_CREDENTIALS_FILE)\b"
    rf"|\$\{{?XDG_CONFIG_HOME\}}?/(gcloud|gh){_SEG_END}")
SECRET_HINTS = re.compile(
    CREDENTIAL_HINTS.pattern
    + r"|\.env\b|credentials|\.npmrc|_history\b|find-generic-password|find-internet-password")


def sensitivity(path: str | Path) -> str | None:
    """路径落在哪一档：``"deny"`` / ``"ask"`` / ``None``。用于目录遍历时逐个文件判断（权限层只看得到遍历的根）。"""
    s = str(path)
    if any(fnmatch.fnmatchcase(s, g) for g in CREDENTIAL_GLOBS):
        return "deny"
    if any(fnmatch.fnmatchcase(s, g) for g in SECRET_HINT_GLOBS):
        return "ask"
    return None


__all__ = ["CREDENTIAL_GLOBS", "CREDENTIAL_HINTS", "SECRET_HINT_GLOBS", "SECRET_HINTS", "sensitivity"]
