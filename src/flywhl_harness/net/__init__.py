"""联网工具的网络层：出站客户端（``client``）、后端适配与故障转移（``search``、``backends``）、外传检查（``exfil``）、
正文抽取（``extract``）。工具本身在 ``tools/web.py``。

这里只给 ``web_search`` / ``web_fetch`` 用。bash 里的命令走沙箱与 ``tools/egress.py`` 的出站代理，两条通道各管各的：
把搜索主机写进 ``permissions.network_allow`` 等于同时放开 ``bash curl``。
"""

from .client import Fetched, WebClient, WebError
from .search import SearchHit, SearchQuery, WebService

__all__ = ["Fetched", "SearchHit", "SearchQuery", "WebClient", "WebError", "WebService"]
