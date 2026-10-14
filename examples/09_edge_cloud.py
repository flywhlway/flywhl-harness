"""09 · 端云协同：云端“大脑”向端侧“双手”下发指令，网络会丢包、重复、乱序、断线。

运行：python examples/09_edge_cloud.py

机制：幂等键去重 + ACK 状态机（pending → sent → acked/failed/expired）+ 超时重发 + 离线排队 + TTL，
端侧用“执行去重表”实现幂等消费——于是“至少一次投递”变成“效果上恰好一次”。
"""

import asyncio

from _common import G, X, say, title

from flywhl_harness.runtime.dispatch import CommandDispatcher, EdgeExecutor, FlakyTransport


async def main() -> None:
    title("09 端云指令下发")
    net = FlakyTransport(drop=0.3, dup=0.25, max_delay=0.03, seed=2026)
    clock = [0.0]
    cloud = CommandDispatcher(net, ack_timeout=1.0, max_attempts=20, clock=lambda: clock[0])
    ran: list[int] = []

    async def run_on_device(payload):  # 端侧真正执行副作用的地方（如：打开车窗、写文件）
        ran.append(payload["step"])
        return f"step {payload['step']} ok"

    edge = EdgeExecutor("laptop-01", net, run_on_device)
    net.device, net.cloud = edge, cloud

    cmds = [cloud.submit("laptop-01", {"step": i}, idem_key=f"run42:call{i}") for i in range(10)]
    dup = cloud.submit("laptop-01", {"step": 0}, idem_key="run42:call0")
    say(f"重复提交同一幂等键 → 返回同一条指令：{dup is cmds[0]}")

    say("端侧离线：指令留在队列里")
    await cloud.pump()
    print(f"  已发送 {net.stats['sent']} 条，状态：{[c.status for c in cmds][:3]}…")

    say("端侧上线：开始投递（丢包 30%、重复 25%、随机延迟导致乱序）")
    cloud.set_online("laptop-01", True)
    rounds = 0
    while not all(c.status == "acked" for c in cmds) and rounds < 60:
        await cloud.pump()
        await net.drain()
        clock[0] += 1.1  # 推进时钟：触发 ACK 超时重发
        rounds += 1
    print(f"  {rounds} 轮投递；网络统计：{net.stats}")
    print(f"  每条指令的投递次数：{[c.attempts for c in cmds]}")
    print(f"  {G}端侧实际执行：{sorted(ran)}（共 {len(ran)} 次，每条恰好一次）{X}")
    print(f"  状态历史示例：{cmds[3].history}")


if __name__ == "__main__":
    asyncio.run(main())
