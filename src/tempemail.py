"""CF Temp Email Worker 客户端。

关键设计（沿用参考项目踩过的坑）：

- 收信走**按收件人索引**的端点 `GET /api/inbox?email=`，不走 `/admin/all`
  （拉最新 N 条再自己筛）。该 Worker 可能被同机其它项目共用，retention 是
  「全表 100 个 id 的窗口」，拉列表会把我们的邮件挤出窗口。
- **5xx 要重试**：那是「服务端现在读不出来」，不是「邮件没到」。4xx 立刻失败。
- 轮询计数（`polls` / `http_5xx`）要上报 —— 否则「读不出来」与「站点没发」
  只能靠猜，而两者的处置恰好相反（修 Worker / 重发信）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

from . import config


class TempMailError(RuntimeError):
    pass


def _parse_ts(v: Any) -> int:
    """旧版 Worker 的 created_at 是 UTC 字符串，不是时间戳。

    转不成毫秒会让 received_at=0，随后被 since_ms 基线整体滤掉 ——
    表现是「邮件明明到了却一直等不到」。
    """
    if isinstance(v, (int, float)):
        n = int(v)
        return n * 1000 if 0 < n < 10 ** 12 else n
    s = str(v or "").strip()
    if not s:
        return 0
    from datetime import datetime, timezone
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def _decode_raw_mime(raw: str) -> tuple[str, str, str]:
    """旧版 Worker 只回 raw MIME：解出 (subject, from, body)。"""
    import email as _email
    from email.header import decode_header as _dh
    try:
        msg = _email.message_from_string(raw)
    except Exception:
        return "", "", raw
    def dec(v: str) -> str:
        try:
            return "".join(x.decode(e or "utf-8", "replace") if isinstance(x, bytes) else x
                           for x, e in _dh(v))
        except Exception:
            return str(v)
    subj = dec(msg.get("Subject") or "")
    frm = dec(msg.get("From") or "")
    parts = []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            try:
                parts.append(part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", "replace"))
            except Exception:
                pass
    return subj, frm, "\n".join(parts)


@dataclass
class Stats:
    """只留真的会被读的计数器（它们出现在超时诊断文案里）。"""

    polls: int = 0
    http_5xx: int = 0


@dataclass
class Mail:
    id: str
    to: str
    sender: str
    subject: str
    body: str
    received_at: int

    @property
    def recipient(self) -> str:
        return self.to


def _as_int(v: Any) -> int:
    """容错转 int。

    Worker 的 `received_at` 正常是毫秒时间戳，但它是上游写入的字段：
    一条畸形记录不该让**整轮收信**抛 ValueError（那会把「读不出来」伪装成
    「邮件没到」，把排查引向重发信）。转不了就当 0（= 最旧），排序时排在前面。
    """
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


class TempMailClient:
    def __init__(self, base: str | None = None, admin_key: str | None = None,
                 session: requests.Session | None = None):
        self.base = (base or config.TEMPMAIL_BASE).rstrip("/")
        self.admin_key = (admin_key if admin_key is not None
                          else config.TEMPMAIL_ADMIN_KEY)
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": config.UA, "Accept": "application/json"})
        self.stats = Stats()
        self._jwt_map: dict[str, str] = {}
        self._legacy: bool | None = None

    def _is_legacy(self) -> bool:
        """cloudflare_temp_email v1.x（/api/new_address + /api/mails）判定。"""
        if self._legacy is None:
            try:
                r = self.s.get(f"{self.base}/open_api/settings", timeout=15)
                self._legacy = (r.status_code == 200 and "domains" in (r.text or ""))
            except Exception:
                self._legacy = False
        return self._legacy

    # ── 底层 ──────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, *, retries: int = 4,
                 backoff: float = 0.8, **kw) -> requests.Response:
        """5xx 重试、4xx 立刻失败。"""
        for attempt in range(retries):
            try:
                r = self.s.request(method, f"{self.base}{path}", timeout=30, **kw)
            except requests.RequestException as exc:
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                    continue
                raise TempMailError(f"网络失败 {path}: {exc}") from exc

            if 500 <= r.status_code < 600:
                self.stats.http_5xx += 1
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                    continue
                raise TempMailError(
                    f"邮箱服务持续 5xx（{self.stats.http_5xx} 次，"
                    f"最近 HTTP {r.status_code}）—— 不是邮件没到，是读不出来"
                )
            if 400 <= r.status_code < 500:
                raise TempMailError(f"HTTP {r.status_code} {path}: {r.text[:200]}")
            return r
        raise TempMailError(f"重试耗尽: {path}")

    def health(self) -> dict[str, Any]:
        if self._is_legacy():
            return self._request("GET", "/open_api/settings").json()
        return self._request("GET", "/health").json()

    # ── 建邮箱 ────────────────────────────────────────────────────────
    def create_mailbox(self, domain: str | None = None) -> str:
        if self._is_legacy():
            d = domain or config.TEMPMAIL_DOMAIN
            headers = {"Content-Type": "application/json"}
            body: dict[str, Any] = {"enablePrefix": True}
            if d:
                body["domain"] = d
            if self.admin_key:
                headers["x-admin-auth"] = self.admin_key
                body["name"] = "tj" + str(int(time.time() * 1000))[-7:]
            r = self._request("POST", "/api/new_address",
                              headers=headers, data=json.dumps(body))
            data = r.json()
            addr = str(data.get("address") or "").strip()
            jwt = data.get("jwt")
            if not addr:
                raise TempMailError(f"旧版建邮箱未返回地址: {str(data)[:300]}")
            if jwt:
                self._jwt_map[addr] = str(jwt)
            return addr

        body: dict[str, Any] = {}
        d = domain or config.TEMPMAIL_DOMAIN
        if d:
            body["domain"] = d
        r = self._request(
            "POST", "/api/mailboxes",
            headers={"Authorization": f"Bearer {self.admin_key}",
                     "Content-Type": "application/json"},
            data=json.dumps(body),
        )
        data = r.json()
        emails = data.get("emails") or (data.get("data") or {}).get("emails") or []
        if not emails:
            raise TempMailError(f"建邮箱未返回地址: {str(data)[:300]}")
        return str(emails[0]).strip()

    # ── 收信 ──────────────────────────────────────────────────────────
    def list_mails(self, email: str | None = None,
                   limit: int | None = None) -> list[Mail]:
        """旧版 Worker 走 /api/mails（地址级 jwt）；新版走 /api/inbox。"""
        if self._is_legacy():
            self.stats.polls += 1
            jwt = self._jwt_map.get(email or "")
            if not jwt:
                raise TempMailError(f"缺少 {email} 的地址凭证 jwt（旧版 Worker 需先建邮箱）")
            r = self._request("GET", "/api/mails",
                              params={"limit": limit or 100, "offset": 0},
                              headers={"Authorization": f"Bearer {jwt}"})
            data = r.json() or {}
            msgs = data.get("results") if isinstance(data, dict) else data
            msgs = msgs or []
            out: list[Mail] = []
            for m in msgs:
                # 旧版 /api/mails 已按 jwt 地址索引，只回本地址邮件；
                # 其 address 字段可能是发件人，不能再拿它做收件人过滤。
                to = str(m.get("to") or email or "")
                raw = m.get("raw") or ""
                if raw:
                    subj, frm, bod = _decode_raw_mime(raw)
                else:
                    subj, frm, bod = (m.get("subject") or "", m.get("from") or "",
                                      m.get("text") or m.get("body") or "")
                out.append(Mail(
                    id=str(m.get("id", "")), to=to or (email or ""),
                    sender=frm, subject=subj, body=bod,
                    received_at=_parse_ts(m.get("created_at") or m.get("received_at")),
                ))
            out.sort(key=lambda x: x.received_at)
            return out

        if email:
            self.stats.polls += 1
            r = self._request("GET", "/api/inbox", params={"email": email})
            msgs = (r.json() or {}).get("messages") or []
        else:
            self.stats.polls += 1
            r = self._request("GET", "/admin/all",
                              params={"limit": limit or 20},
                              headers={"Authorization": f"Bearer {self.admin_key}"})
            body = r.json() or {}
            msgs = body.get("messages") if isinstance(body, dict) else body
            msgs = msgs or []

        out: list[Mail] = []
        for m in msgs:
            out.append(Mail(
                id=str(m.get("id", "")),
                to=m.get("to_address") or m.get("to") or "",
                sender=m.get("from_address") or m.get("from") or "",
                subject=m.get("subject") or "",
                body=(m.get("body") or m.get("body_text")
                      or m.get("text") or m.get("raw_text") or ""),
                received_at=_as_int(m.get("received_at")),
            ))
        out.sort(key=lambda x: x.received_at)
        return out

    def wait_for_mail(self, email: str, match: Callable[[Mail], bool], *,
                      timeout: float = 90.0, interval: float = 0.5,
                      since_ms: int | None = None) -> Mail | None:
        """轮询等一封满足条件的邮件。

        `since_ms` 用于跳过历史邮件 —— 同一个地址可能已经收过旧链接。
        读信要当场读走：Worker 的 retention 是全表 100 行。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            for m in self.list_mails(email=email):
                if since_ms is not None and m.received_at < since_ms:
                    continue
                if match(m):
                    return m
            time.sleep(interval)
        return None

    def scan_all(self, limit: int = 100) -> list[Mail]:
        """扫整个窗口（不按收件人过滤），**只用于诊断**。

        常规收信一律走 `list_mails(email=...)` 的索引端点：
        这条通路会占满窗口读取额度，且服务端对 `limit` 有硬上限。
        """
        return self.list_mails(email=None, limit=limit)
