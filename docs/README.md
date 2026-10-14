# flywhl-harness 文档站点

Agent Harness 系列教程 + flywhl-harness 源码讲解，基于 VitePress，可独立构建与部署。

## 本地预览

```bash
npm install
npm run dev        # http://localhost:5173
```

## 构建

```bash
npm run build      # 输出到 .vitepress/dist（纯静态文件）
npm run preview    # 本地预览构建产物
```

部署在子路径下时设置 `BASE`，例如 `BASE=/flywhl-harness/ npm run build`。

## 打包

一键构建并把产物打成压缩包，输出到 `release/`（已 gitignore）：

```bash
npm run pack:site                  # → release/flywhl-harness-docs-v<版本>-<commit>.tar.gz
npm run pack:site -- --zip         # 改为 .zip
npm run pack:site -- --skip-build  # 不重新构建，直接打包现有 .vitepress/dist
npm run pack:site -- -o <目录>     # 指定输出目录
```

- 包内有一层同名顶层目录，解压后把它作为站点根目录即可；工作区有未提交改动时包名带 `-dirty`。
- 同时生成 `.sha256` 校验文件，传输后可用 `shasum -a 256 -c <包名>.sha256` 校验。
- `BASE` 同样透传，例如 `BASE=/flywhl-harness/ npm run pack:site`。

## 部署

产物是纯静态文件，任何静态托管都可以：

| 平台 | 配置 |
|---|---|
| GitHub Pages | 仓库已带 `.github/workflows/deploy-docs.yml`；Settings → Pages → Source 选 GitHub Actions |
| Netlify / Vercel / Cloudflare Pages | 根目录 `docs`，构建命令 `npm run build`，输出目录 `.vitepress/dist` |
| Nginx / 对象存储 | 上传 `.vitepress/dist` 目录；开启 `try_files $uri $uri.html $uri/ =404;` 以支持无扩展名 URL |

## 目录

```
guide/          入门：什么是 Harness、快速上手、架构总览
core/           核心机制：Loop、消息与模型、工具、上下文、终止与预算
reliability/    可靠性：状态、状态机、恢复、取消、重试、幂等、流式
governance/     策略与执行环境：Hooks、权限与人审、Workspace 与沙箱、安全
multi-agent/    多 Agent 与度量：子代理、DAG、轨迹与评测、可观测与成本
advanced/       进阶：长时任务、持久执行、端云、协议、可删减 Harness、对照 pi、扩展
reference/      知识地图、动手练习、源码地图、术语表、参考资料
```
