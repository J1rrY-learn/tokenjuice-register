# 本地适配补丁（2026-11-12）

原仓库：https://github.com/2951461586/Tokenjuice-Register-Tool
原版最后提交：2026-10-23。站点在此之后改了两处，原版直接跑会 100% 卡在等信。

## 改了什么

1. **`src/mailrules.py` — 发件人域**
   站点已把 From 从 `xxx@us-west-2.amazonses.com` 改成 `no-reply@tokenjuice.ai`。
   原规则只认 `amazonses.com`，导致邮件明明到了却判「未收到」。
   现在两条都认。

2. **`src/tempemail.py` — 旧版 Worker 适配**
   本机现成的 CF Worker（cloudflare_temp_email v1.x）只有
   `POST /api/new_address`（地址级 jwt）+ `GET /api/mails`，
   而原版按新版 API（`/api/mailboxes` + `/api/inbox?email=`）写。
   已加自动探测 `_is_legacy()`，两种 Worker 都能用。

3. **`src/tempemail.py` — 收件人过滤**
   旧版 `/api/mails` 的 `address` 字段是发件人，不能拿来做收件人过滤
   （会把本地址邮件全部排除）。旧版改为只信任 jwt 索引。

4. **`src/tempemail.py` — 时间解析**
   旧版 `created_at` 是 UTC 字符串（`2026-11-11 20:29:28`），原版 `_as_int` 转成 0，
   随后被 `since_ms` 基线整体过滤 → 表现同样是「等不到信」。已加 `_parse_ts`。

5. **`src/config.py` — admin key 可选**
   旧版 Worker 支持匿名建址，不需要 `TEMPMAIL_ADMIN_KEY`。

## 当前 .env

```
TEMPMAIL_ADMIN_KEY=
TEMPMAIL_BASE=https://mailapi.kitakerja.eu.org
TEMPMAIL_DOMAIN=kitakerja.eu.org
```

`kitakerja` 是第三方公共邮箱服务，域名可能被站点拉黑或限流。
更稳的做法是用自己的 Worker（`apimail.appstoy.com` / `196856.xyz`），
但它的 admin password 已经不是 `appstoy.com` 了，需要找回。

## 已验证

```
[1/1] q960tjz8ai@kitakerja.eu.org  keyed  mailbox/send/mail/verify/me/api_key 全 ok
      total 18.5s
verify_keys: models=200 chat=200 model=deepseek-ai/DeepSeek-V4.1-Flash-1017
直接调用 chat/completions → HTTP 200, content="OK"
```
