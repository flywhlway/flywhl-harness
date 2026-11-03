// 首帧就用正确的主题，避免明暗闪烁。必须是外链脚本：CSP 的 script-src 'self' 不执行任何内联脚本
try { document.documentElement.dataset.theme = localStorage.getItem("flywhl-theme") || "system"; } catch { /* 隐私模式等场景读不到存储 */ }
