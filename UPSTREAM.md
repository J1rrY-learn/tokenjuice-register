# 来源与本仓库的改动

- 上游：https://github.com/2951461586/Tokenjuice-Register-Tool
- 上游最后提交：2026-10-23
- 本仓库：在其基础上做了**站点变更适配**与**注册提速**，见 `PATCHES.md`

## 为什么需要改

上游发布后，`tokenjuice.ai` 侧改了两处，原版直接跑会 100% 卡在「等信」：

1. 发件人从 `xxx@us-west-2.amazonses.com` 改成 `no-reply@tokenjuice.ai`
   → `src/mailrules.py` 的硬编码 SES 域不匹配
2. 旧版 CF Worker 的 `created_at` 是 UTC 字符串，不是时间戳
   → 被 `since_ms` 基线整体过滤

## 本仓库额外能力

- **两代 CF Temp Email Worker 自动适配**（`_is_legacy()` 探测）
- **`--skip-verify`**：注册时跳过网关验活，把 8~9s/号 的 `verify_key` 挪到
  跑完统一做（`verify_keys.py`），单号 13~17s → 7.7s
- 并发实测：`--concurrency 4` 稳定 ~14.7 号/分钟，0 失败

## 实测数据（2026-11-12，成都电信家宽出口）

| 批次 | 结果 | 备注 |
| --- | --- | --- |
| 10 @1 | 10/10 | 8.5~9.8s/号 |
| 100 @2 | 100/100 | ~5 分钟 |
| 400 @2 | 400/400 | ~15 分钟 |
| 1044 累计 | 0 失败 | `--skip-verify` 前后各一半 |

> 上游 README 的「发件人域稳定」假设已失效，本仓库按实测修正。
