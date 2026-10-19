#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""贴吧一键签到脚本。

用 BDUSS(可带 STOKEN)登录，自动取出「我关注的贴吧」列表并逐个签到，
也可只签指定吧。支持重试、限速、指定吧、dry-run、JSON 输出。

用法:
    python tieba_sign.py --check               # 先自检：环境 / 代理 / 两种 HTTP 后端 / cookie 是否有效
    python tieba_sign.py                       # 签到所有关注的吧
    python tieba_sign.py --forums 李毅站 原神吧  # 只签这几个吧
    python tieba_sign.py --list                # 只列出关注的吧
    python tieba_sign.py --dry-run             # 不签到，只看会签哪些吧
    python tieba_sign.py --retry-failed        # 对失败的吧再补签一轮

cookie 只能通过命令行、配置文件或环境变量提供，本脚本不读取浏览器数据。

关于 HTTP 后端(重要)：
    同一台机器上，requests 和 urllib 打贴吧的成功率可能完全不同——
    有的网络环境下 requests 会一直 ReadTimeout（TLS 指纹被边缘节点拦），
    而标准库 urllib 秒回。反过来也有 requests 正常、urllib 被代理劫持的情况。
    所以这里两种后端都实现，默认 --transport auto：先探测，谁通用谁。
"""

from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import random
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

HOME = "https://tieba.baidu.com"
SIGN_PATH = "/sign/add"
FOLLOW_PATH = "/f/like"
TBS_PATH = "/dc/common/tbs"
# 移动端接口（油猴脚本走的就是这条）。与 Web 接口的关键差别：
#   1) 它真的校验 sign，而 /sign/add 不校验（实测：签名改错/不传，返回完全相同）；
#   2) 签名算法不同——单层 md5、原样拼接、值不编码，见 sign_client()；
#   3) 多返回一个 user_sign_rank（本吧签到排名）。
MOBILE_HOME = "https://c.tieba.baidu.com"
MOBILE_SIGN_PATH = "/c/c/forum/sign"
# 一次拿到「关注列表 + 每个吧是否已签 + tbs」，省掉翻页和一堆无用请求
MOBILE_LIST_PATH = "/mo/q/newmoindex"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "config.json"
DEFAULT_COOKIE_FILE = SCRIPT_DIR / "cookies.txt"

# 移动端签名的固定盐。注意：两种签名算法共用它，但 preimage 完全不同。
SIGN_SALT = "tiebaclient!!!"


def sign_client(forum: str, tbs: str) -> str:
    """移动端签到签名：md5("kw=吧名tbs=...tiebaclient!!!")。

    单层 md5、参数原样拼接、值**不做 URL 编码**。这里不能替成 _sign()——
    拿 Web 的排序+编码签名去请求移动端接口，服务端会直接以 110001 拒掉（实测）。
    """
    import hashlib

    raw = f"kw={forum}tbs={tbs}{SIGN_SALT}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def sign_web(params: Dict[str, str]) -> str:
    """Web 端签名：对排序后的 k=v 对（值需 encodeURIComponent）套两层 md5。

    实测 /sign/add **并不校验**这个签名（写错、甚至不传，返回都一样），
    所以它在本脚本里属于「照抄前端、以防将来百度开始校验」，不是功能依赖。
    """
    import hashlib

    parts = [f"{k}={quote(str(v), safe='')}" for k, v in params.items()]
    joined = "".join(sorted(parts))
    first = hashlib.md5(joined.encode("utf-8")).hexdigest()
    return hashlib.md5((first + SIGN_SALT).encode("utf-8")).hexdigest()


try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": f"{HOME}/",
    "X-Requested-With": "XMLHttpRequest",
}

# 签到接口返回码 -> 中文说明
SIGN_MESSAGES = {
    0: "签到成功",
    1101: "今天已签到",
    1102: "签到失败",
    1103: "签到过快",
    1104: "签到过快",
    1106: "该吧已不存在",    1107: "该吧已被封禁",
    1108: "该吧目录已满",
    1109: "该吧拒绝加入",
    1110: "未加入该吧",
    1111: "该吧人数已达上限",
    1011: "未加入该吧",
    1010: "tbs 失效",
    1990055: "账号未实名/登录态不足（多为无有效 BDUSS）",
    2150040: "需要验证码(账号被限流)",
    2150041: "账号被限流",
    340006: "账号已被封禁",
    160002: "今天已签到",
    1: "未登录 / BDUSS 失效",
    2: "未登录 / BDUSS 失效",
    4: "未登录 / BDUSS 失效",
    5: "BDUSS 失效或账号异常",
    7: "未登录 / BDUSS 失效",
    10: "未登录 / BDUSS 失效",
}
# 视为成功的返回码（移动端的「已签到」是 160002，Web 端是 1101，两边都收）
SIGN_OK = {0, 1101, 160002}
# 视为登录失效的返回码（不再重试其它吧，直接中止）
SIGN_FATAL = {1, 2, 4, 5, 7, 10, 340006}
# 移动端接口「签名被拒」的返回码（实测：签名算法不对时报这个）
# 注意移动端也用 1 表示「未登录」——所以这里的 1 必须靠 error_msg 区分，
# 不能直接并进 SIGN_FATAL，否则会把「签名算错」误报成「登录失效」。
MOBILE_SIGN_BAD_SIGN = {110001}


# --------------------------------------------------------------------------- #
# HTTP 传输层：两种后端，统一的取值 / 解码接口
# --------------------------------------------------------------------------- #
@dataclass
class HttpResult:
    """一次 HTTP 响应，只保留业务需要的东西。"""

    status: int
    body: str
    elapsed: float
    backend: str = ""

    def json(self) -> Any:
        """把响应体当 JSON 解析；不是 JSON 时给出可操作的报错。"""
        text = self.body.strip()
        if not text:
            raise RuntimeError(f"接口返回空响应（HTTP {self.status}，后端 {self.backend}）")
        try:
            return json.loads(text)
        except ValueError:
            head = re.sub(r"\s+", " ", text[:200])
            if "<html" in text[:400].lower() or "<!doctype" in text[:400].lower():
                raise RuntimeError(
                    f"接口返回了 HTML 而不是 JSON（HTTP {self.status}，后端 {self.backend}）——"
                    f"多半是登录态失效或触发了风控/验证码。正文开头：{head!r}"
                ) from None
            raise RuntimeError(
                f"接口返回的不是 JSON（HTTP {self.status}，后端 {self.backend}）：{head!r}"
            ) from None


class RequestsBackend:
    """requests 后端。走系统代理，故默认关闭 trust_env。"""

    name = "requests"

    def __init__(self, cookie: Dict[str, str], timeout: float, use_proxy: bool = False):
        if requests is None:
            raise RuntimeError("没有安装 requests，无法使用该后端（可用 --transport urllib）")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = use_proxy
        if not use_proxy:
            self.session.proxies = {"http": None, "https": None}
        self.session.cookies.update(cookie)

    def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResult:
        hdrs = dict(DEFAULT_HEADERS)
        hdrs.update(headers or {})
        started = time.time()
        if method == "POST":
            resp = self.session.post(url, data=data, headers=hdrs, timeout=self.timeout)
        else:
            resp = self.session.get(url, params=params, headers=hdrs, timeout=self.timeout)
        return HttpResult(resp.status_code, resp.text, time.time() - started, self.name)


class UrllibBackend:
    """标准库 urllib 后端：不读任何代理配置，纯直连。

    之所以需要它：部分网络下 requests 打贴吧会稳定 ReadTimeout，而 urllib 正常。
    """

    name = "urllib"

    def __init__(self, cookie: Dict[str, str], timeout: float, use_proxy: bool = False):
        self.timeout = timeout
        proxy = urllib.request.ProxyHandler({} if not use_proxy else None)
        self.cookie_header = "; ".join(f"{k}={v}" for k, v in cookie.items())
        ctx = ssl.create_default_context()
        self.opener = urllib.request.build_opener(
            proxy, urllib.request.HTTPSHandler(context=ctx)
        )

    def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResult:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        hdrs = dict(DEFAULT_HEADERS)
        hdrs.update(headers or {})
        hdrs["Cookie"] = self.cookie_header
        body = urllib.parse.urlencode(data).encode("utf-8") if data else None
        if method == "POST":
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        started = time.time()
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                # 贴吧部分页面是 GBK，用 utf-8 降级替换，避免整段崩掉
                return HttpResult(
                    resp.status, raw.decode("utf-8", "replace"),
                    time.time() - started, self.name,
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if exc.headers.get("Content-Encoding") == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            return HttpResult(
                exc.code, raw.decode("utf-8", "replace"),
                time.time() - started, self.name,
            )


BACKENDS = {"requests": RequestsBackend, "urllib": UrllibBackend}


def build_backend(
    name: str, cookie: Dict[str, str], timeout: float, use_proxy: bool = False
):
    if name not in BACKENDS:
        raise RuntimeError(f"未知的 HTTP 后端 {name!r}，可选: {', '.join(BACKENDS)}")
    return BACKENDS[name](cookie, timeout, use_proxy)


def probe_backend(
    backend: Any, timeout: Optional[float] = None
) -> Tuple[bool, str, float]:
    """用一个轻量请求判断后端是否可用：能拿到贴吧业务 JSON 就算通。"""
    started = time.time()
    try:
        result = backend.request("GET", f"{HOME}{TBS_PATH}")
        data = result.json()
        if "tbs" not in data:
            return False, f"响应里没有 tbs: {result.body[:80]!r}", time.time() - started
        return True, f"HTTP {result.status}，拿到 tbs", time.time() - started
    except Exception as exc:  # noqa: BLE001 - 探测就是要吞掉所有异常
        return False, f"{type(exc).__name__}: {exc}"[:160], time.time() - started


def pick_backend(
    transport: str,
    cookie: Dict[str, str],
    timeout: float,
    use_proxy: bool,
    verbose: bool = True,
):
    """按需选择可用的 HTTP 后端，返回 (backend, 说明文字)。"""
    if transport != "auto":
        return build_backend(transport, cookie, timeout, use_proxy), f"指定 {transport}"

    order = ["requests", "urllib"] if requests is not None else ["urllib"]
    tried: List[str] = []
    for name in order:
        try:
            backend = build_backend(name, cookie, min(timeout, 8.0), use_proxy)
        except RuntimeError as exc:
            tried.append(f"{name}: 不可用({exc})")
            continue
        ok, detail, elapsed = probe_backend(backend)
        tried.append(f"{name}: {detail} ({elapsed:.2f}s)")
        if ok:
            # 探测用的短超时不能留给正式请求
            return build_backend(name, cookie, timeout, use_proxy), f"自动选中 {name}"
        if verbose:
            print(f"  · 后端 {name} 探测失败：{detail}")
    raise RuntimeError(
        "两种 HTTP 后端都连不上贴吧。探测结果：\n    " + "\n    ".join(tried)
    )


# --------------------------------------------------------------------------- #
# 结果数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Item:
    """一个吧的签到结果。"""

    forum: str
    status: str  # ok | already | failed | error | missed
    code: Optional[int] = None
    message: str = ""
    # 本吧签到排名（移动端接口才会返回，Web 接口没有这个字段）
    rank: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "already")


@dataclass
class Stats:
    ok: int = 0
    already: int = 0
    failed: int = 0
    error: int = 0
    missed: int = 0
    items: List[Item] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.ok + self.already + self.failed + self.error + self.missed


# --------------------------------------------------------------------------- #
# cookie / 配置
# --------------------------------------------------------------------------- #
def parse_cookie(raw: str) -> Dict[str, str]:
    """把浏览器里复制的整串 cookie 或裸 BDUSS 解析成字典。"""
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return {}
    if "=" not in raw:
        # 只给了裸 BDUSS
        return {"BDUSS": raw}
    jar: Dict[str, str] = {}
    for part in re.split(r"[;\n]", raw):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if key:
            jar[key] = value
    return jar


def parse_netscape_cookie_file(path: Path) -> Dict[str, str]:
    """解析 curl/wget 导出的 Netscape 格式 cookie 文件。"""
    jar: Dict[str, str] = {}
    if not path.is_file():
        return jar
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            jar[parts[5].strip()] = parts[6].strip()
    return jar


def load_config(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"警告：读取配置 {path} 失败({exc})，忽略。", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def resolve_cookie(args: argparse.Namespace, config: Dict[str, Any]) -> Dict[str, str]:
    """按 命令行 > 环境变量 > 配置文件 > cookie 文件 的优先级取 cookie。

    配置里的 cookie 支持三种写法：整串字符串、字典、或顶层直接放 BDUSS。
    """
    if args.cookie:
        return parse_cookie(args.cookie)
    env = os.environ.get("TIEBA_COOKIE") or ""
    if env.strip():
        return parse_cookie(env)

    cfg = config.get("cookie")
    if cfg is None:
        cfg = config.get("BDUSS") or ""
    if isinstance(cfg, dict):
        # {"cookie": {"BDUSS": "...", "STOKEN": "..."}}：直接当 cookie 字典用，
        # 顺手把值里的引号/空白清掉，免得复制时带上多余字符
        jar = {
            str(k).strip(): str(v).strip().strip('"').strip("'")
            for k, v in cfg.items()
            if str(k).strip() and str(v).strip()
        }
        if jar:
            return jar
    elif isinstance(cfg, str) and cfg.strip():
        return parse_cookie(cfg)

    jar = parse_netscape_cookie_file(DEFAULT_COOKIE_FILE)
    if jar:
        return jar
    return {}


# --------------------------------------------------------------------------- #
# 贴吧客户端
# --------------------------------------------------------------------------- #
class TiebaClient:
    def __init__(
        self,
        backend: Any,
        cookie: Dict[str, str],
        retries: int = 3,
        timeout: float = 15.0,
        sign_mode: str = "auto",
    ) -> None:
        self.backend = backend
        self.cookie = cookie
        self.retries = max(1, retries)
        self.timeout = timeout
        self.tbs: Optional[str] = None
        # mobile = 走移动端接口（真校验签名，带排名）；web = 走 /sign/add；
        # auto（默认）= 先用 mobile，被拒签名再退回 web，避免接口变了就整轮全废
        self.sign_mode = sign_mode if sign_mode in ("mobile", "web", "auto") else "auto"
        self.effective_mode = self.sign_mode
        # 接口中途切换时记一笔 (code, message)，供上层提示
        self.mode_switched: Optional[Tuple[Optional[int], str]] = None
        # 关注列表里「今天已签到」的吧（只有移动端列表接口能给）
        self.forums_signed: set = set()

    @property
    def backend_name(self) -> str:
        return getattr(self.backend, "name", "?")

    # -- 底层请求：网络错误 / 5xx 自动退避重试 -------------------------------
    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> HttpResult:
        url = path if path.startswith("http") else f"{HOME}{path}"
        last_exc: Optional[Exception] = None
        result: Optional[HttpResult] = None
        for attempt in range(self.retries):
            try:
                result = self.backend.request(method, url, params, data, headers)
            except Exception as exc:  # noqa: BLE001 - 网络层异常一律重试
                last_exc = exc
                if attempt < self.retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                continue
            if result.status >= 500 and attempt < self.retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            return result
        if result is not None:
            return result
        raise RuntimeError(
            f"请求 {url} 失败（后端 {self.backend_name}，重试 {self.retries} 次）: {last_exc}"
        )

    def _get(self, path: str, params: Dict[str, Any]) -> HttpResult:
        return self._request("GET", path, params=params)

    # -- 1) 拉取「我关注的吧」 ------------------------------------------------
    def fetch_forums_status(self) -> Tuple[List[str], set]:
        """返回 (吧名列表, 今天已签到的吧名集合)。

        优先走移动端接口 /mo/q/newmoindex：**一次**调用就带回关注列表，
        而且每个吧带 is_sign（今天签没签）。原来的 /f/like 要翻页，且不带签到状态。
        移动端失败就退回原来的翻页实现，已签集合为空（届时靠签到返回码判断）。
        """
        try:
            result = self._get(
                f"{MOBILE_HOME}{MOBILE_LIST_PATH}",
                {"t": int(time.time() * 1000)},
            )
            data = result.json()
            if int(data.get("no", 1)) == 0:
                rows = (data.get("data") or {}).get("like_forum") or []
                forums: List[str] = []
                signed: set = set()
                for row in rows:
                    name = html.unescape(str(row.get("forum_name", "")).strip())
                    if not name:
                        continue
                    if name not in forums:
                        forums.append(name)
                    if row.get("is_sign") == 1:
                        signed.add(name)
                if forums:
                    self.forums_signed = signed
                    return forums, signed
        except Exception:  # noqa: BLE001 - 移动端接口不可用就退回老路
            pass
        return self.fetch_forums(), set()

    def fetch_forums(self) -> List[str]:
        forums: List[str] = []
        seen = set()
        page = 1
        max_page = 200
        skips = 0
        while page <= max_page:
            result = self._get(
                FOLLOW_PATH,
                {
                    "kw": "",
                    "ie": "utf-8",
                    "pn": page,
                    "_": int(time.time() * 1000),
                },
            )
            try:
                data = result.json()
            except RuntimeError as exc:
                # 不带正确登录态时这个接口可能直接吐 HTML；若整个第一页都不是 JSON，
                # 就是登录态问题而不是分页问题。
                raise RuntimeError(f"获取关注列表失败：{exc}") from None

            code = data.get("no", data.get("error_code"))
            try:
                code = int(code) if code is not None else None
            except (TypeError, ValueError):
                code = None
            if code not in (0, None):
                raise RuntimeError(
                    f"获取关注列表失败: no={code} "
                    f"error={data.get('error') or data.get('errmsg') or ''}"
                    "（多为 BDUSS 失效或被风控）"
                )
            rows = (data.get("data") or {}).get("like_forum") or []
            if not rows:
                break
            for row in rows:
                name = html.unescape(str(row.get("forum_name", "")).strip())
                if name and name not in seen:
                    seen.add(name)
                    forums.append(name)
            if len(rows) < 50:
                break
            page += 1
            skips += 1
            if skips > 1:
                time.sleep(0.4)
        return forums

    # -- 2) 拿 tbs -----------------------------------------------------------
    def fetch_tbs(self) -> str:
        if self.cookie.get("tbs"):
            self.tbs = self.cookie["tbs"]
            return self.tbs

        # 首选专用接口：能直接告诉我们是「没登录」还是「拿到了 tbs」
        try:
            result = self._get(TBS_PATH, {"t": int(time.time() * 1000)})
            data = result.json()
            if not data.get("is_login"):
                raise RuntimeError(
                    "贴吧返回 is_login=0，说明当前 cookie 没有登录态——"
                    "BDUSS 失效、抄错，或复制时漏了。请在浏览器重新登录 tieba.baidu.com 后重新取一次。"
                )
            tbs = str(data.get("tbs") or "").strip()
            if tbs and tbs != "0":
                self.tbs = tbs
                return self.tbs
        except RuntimeError:
            raise
        except Exception:  # noqa: BLE001 - 退回首页正则的老办法
            pass

        # 退路：首页正文里的 tbs
        result = self._get("/", {"_": int(time.time() * 1000)})
        if result.status != 200:
            raise RuntimeError(
                f"贴吧首页返回 HTTP {result.status}（后端 {self.backend_name}），"
                "既拿不到 tbs，也说明这个后端/网络被拦了。"
            )
        text = result.body
        m = (re.search(r"['\"]tbs['\"]\s*:\s*['\"]([0-9a-f]{20,})['\"]", text)
             or re.search(r"tbs=([0-9a-f]{20,})", text))
        if not m:
            raise RuntimeError(
                "无法从贴吧首页拿到 tbs，通常是 BDUSS 失效或触发了验证码"
            )
        self.tbs = m.group(1)
        return self.tbs

    # -- 3) 单个吧签到 --------------------------------------------------------
    def _parse_sign_code(self, data: Dict[str, Any]) -> Tuple[Optional[int], str]:
        """两种接口的字段名不同：Web 用 no/error，移动端用 error_code/error_msg。"""
        raw = data.get("no", data.get("error_code"))
        try:
            code = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            code = None
        msg = str(
            data.get("error")
            or data.get("error_msg")
            or data.get("errmsg")
            or data.get("msg")
            or ""
        ).strip()
        return code, msg

    def _sign_mobile(self, forum: str, tbs: str, retries: int) -> Item:
        """移动端接口：sign 会被真正校验，成功时还能拿到本吧签到排名。"""
        sign = sign_client(forum, tbs)
        payload = {"kw": forum, "tbs": tbs, "sign": sign}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        last_item = Item(forum, "failed", None, "未拿到结果")
        for attempt in range(max(1, retries)):
            try:
                data = self._request(
                    "POST", f"{MOBILE_HOME}{MOBILE_SIGN_PATH}", data=payload, headers=headers
                ).json()
            except RuntimeError as exc:
                last_item = Item(forum, "error", None, str(exc))
                time.sleep(1.0 + attempt)
                continue

            code, msg = self._parse_sign_code(data)
            # 移动端「已签到」不是 1101，而是 160002
            if code in (0, 160002):
                status = "ok" if code == 0 else "already"
                rank = ((data.get("user_info") or {}) if isinstance(data, dict) else {}).get(
                    "user_sign_rank"
                )
                try:
                    rank = int(rank) if rank else None
                except (TypeError, ValueError):
                    rank = None
                return Item(forum, status, code, msg or SIGN_MESSAGES.get(code, ""), rank)
            if code in MOBILE_SIGN_BAD_SIGN:
                # 签名被拒 —— 不是这个吧的问题，是接口/签名变了。
                # 在这里就换回 Web 接口，别让整轮都白跑。
                self.effective_mode = "web"
                self.mode_switched = (code, msg or "移动端签名被拒")
                return self._sign_web(forum, tbs, retries)
            if code in SIGN_FATAL:
                return Item(forum, "error", code, msg or SIGN_MESSAGES.get(code, "登录失效"))
            if code in (1103, 1104) and attempt < retries - 1:
                time.sleep(random.uniform(4.0, 6.0))
                last_item = Item(forum, "failed", code, msg or "签到过快")
                continue
            last_item = Item(
                forum,
                "failed",
                code,
                msg or SIGN_MESSAGES.get(code or -1, f"未知返回码 {code}"),
            )
            break
        return last_item

    def _sign_web(self, forum: str, tbs: str, retries: int) -> Item:
        """Web 接口 /sign/add。实测它不校验 sign，但保留原样以免哪天开始校验。"""
        params = {"ie": "utf-8", "kw": forum, "tbs": tbs}
        params["sign"] = self._sign(params)
        last_item = Item(forum, "failed", None, "未拿到结果")
        for attempt in range(max(1, retries)):
            try:
                data = self._request("POST", SIGN_PATH, data=params).json()
            except RuntimeError as exc:
                last_item = Item(forum, "error", None, str(exc))
                time.sleep(1.0 + attempt)
                continue

            code, msg = self._parse_sign_code(data)
            if code in SIGN_OK:
                status = "ok" if code == 0 else "already"
                return Item(forum, status, code, msg or SIGN_MESSAGES.get(code, ""))
            if code in SIGN_FATAL:
                return Item(forum, "error", code, msg or SIGN_MESSAGES.get(code, "登录失效"))
            if code in (1103, 1104) and attempt < retries - 1:
                time.sleep(random.uniform(4.0, 6.0))
                last_item = Item(forum, "failed", code, msg or "签到过快")
                continue
            last_item = Item(
                forum,
                "failed",
                code,
                msg or SIGN_MESSAGES.get(code or -1, f"未知返回码 {code}"),
            )
            break
        return last_item

    def sign_forum(self, forum: str, tbs: str, retries: int = 2) -> Item:
        """按 sign_mode 选接口。

        实测结论：移动端接口**真的校验 sign**（算法不对就 110001 拒掉），
        而 Web 接口 /sign/add 完全不校验。所以优先走移动端——它至少能证明
        请求是合法的；万一哪天移动端接口变了，auto 模式会自己退回 Web。
        """
        if self.effective_mode == "web":
            return self._sign_web(forum, tbs, retries)
        return self._sign_mobile(forum, tbs, retries)

    @staticmethod
    def _sign(params: Dict[str, str]) -> str:
        """兼容旧调用点，实现见 sign_web()。"""
        return sign_web(params)


# --------------------------------------------------------------------------- #
# 签到流程
# --------------------------------------------------------------------------- #
def sign_forums(
    client: TiebaClient,
    forums: List[str],
    delay: float,
    tbs: str,
    skip_signed: Optional[set] = None,
) -> Stats:
    """逐个签到。

    skip_signed 是从移动端列表接口拿到的「今天已签到」集合：命中的吧直接记为
    already 并跳过网络请求。56 个吧能省掉 50 多个无用请求——但**只在你明确
    要求时才用**，因为 is_sign 万一不准就会漏签一天。
    """
    stats = Stats()
    for index, forum in enumerate(forums, 1):
        if skip_signed and forum in skip_signed:
            item = Item(forum, "already", 160002, "列表接口显示今天已签到（已跳过请求）")
            stats.items.append(item)
            stats.already += 1
            print(f"  = ({index}/{len(forums)}) {forum} — {item.message}")
            continue

        item = client.sign_forum(forum, tbs)
        stats.items.append(item)
        if item.status == "ok":
            stats.ok += 1
        elif item.status == "already":
            stats.already += 1
        elif item.status == "error":
            stats.error += 1
        else:
            stats.failed += 1

        mark = {"ok": "✓", "already": "=", "failed": "✗", "error": "!"}.get(
            item.status, "?"
        )
        detail = item.message or ""
        if item.rank:
            detail = f"{detail} 本吧排名 {item.rank}".strip()
        if item.code is not None and str(item.code) not in detail:
            detail = f"[{item.code}] {detail}"
        print(f"  {mark} ({index}/{len(forums)}) {item.forum} — {detail}")
        sys.stdout.flush()

        if item.status == "error" and item.code in SIGN_FATAL:
            rest = forums[index:]
            if rest:
                # 登录已失效：剩下的吧全部标记为未处理
                for name in rest:
                    stats.items.append(
                        Item(name, "missed", item.code, "登录失效，跳过")
                    )
                    stats.missed += 1
                print(f"  ! 登录状态失效，剩余 {len(rest)} 个吧已跳过")
            break

        if index < len(forums):
            time.sleep(delay + random.uniform(0, delay * 0.4))
    return stats


def print_summary(stats: Stats) -> None:
    print("\n" + "=" * 56)
    print(
        f"共 {stats.total} 个吧 | 本次成功 {stats.ok} | 已签过 {stats.already} "
        f"| 失败 {stats.failed} | 异常 {stats.error} | 跳过 {stats.missed}"
    )
    bad = [i for i in stats.items if i.status in ("failed", "error")]
    if bad:
        print("\n需要关注的吧：")
        for item in bad:
            print(f"  - {item.forum}: {item.message} (code={item.code})")
    if not bad:
        print("全部完成，没有需要重试的吧。")
    print("=" * 56)


def write_json(path: Path, stats: Stats, started: float, dry_run: bool) -> None:
    payload = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": round(time.time() - started, 1),
        "dry_run": dry_run,
        "summary": {
            "total": stats.total,
            "ok": stats.ok,
            "already": stats.already,
            "failed": stats.failed,
            "error": stats.error,
            "missed": stats.missed,
        },
        "items": [
            {
                "forum": i.forum,
                "status": i.status,
                "code": i.code,
                "message": i.message,
                "rank": i.rank,
            }
            for i in stats.items
        ],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)
    print(f"结果已写入 {path}")


def write_log(path: Path, stats: Stats, started: float) -> None:
    """追加一行运行记录，供计划任务 / 日志文件使用。时间与语言环境无关。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    elapsed = round(time.time() - started, 1)
    line = (
        f"{stamp} 签到完成 共{stats.total} 成功{stats.ok} 已签{stats.already} "
        f"失败{stats.failed} 异常{stats.error} 跳过{stats.missed} 耗时{elapsed}s"
    )
    bad = [i for i in stats.items if i.status in ("failed", "error")]
    if bad:
        detail = "; ".join(f"{i.forum}({i.message}/code={i.code})" for i in bad[:10])
        more = f" 等{len(bad)}个" if len(bad) > 10 else ""
        line += f" | 待处理: {detail}{more}"
    with path.open("a", encoding="utf-8") as fp:
        fp.write(line + "\n")
    print(f"运行记录已追加到 {path}")


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #
def _describe_proxy() -> str:
    """本机代理配置：这决定了两个后端会不会被劫持。"""
    proxies = urllib.request.getproxies()
    env = {k: v for k, v in os.environ.items() if "proxy" in k.lower()}
    if not proxies and not env:
        return "系统/环境里没有配置代理（直连）"
    parts = []
    if proxies:
        parts.append("urllib 读到的系统代理: " + ", ".join(f"{k}={v}" for k, v in proxies.items()))
    if env:
        parts.append("环境变量: " + ", ".join(f"{k}={v}" for k, v in env.items()))
    if proxies or env:
        host = (proxies or env).get("https") or (proxies or env).get("http") or ""
        m = re.search(r"(\d+)$", str(host))
        if m:
            port = int(m.group(1))
            sock = socket.socket()
            sock.settimeout(1.5)
            try:
                sock.connect(("127.0.0.1", port))
                parts.append(
                    f"→ 本机 {port} 端口有代理在监听（脚本默认绕开它直连；"
                    f"若你的代理是必须的，加 --use-proxy）"
                )
            except OSError:
                parts.append(f"→ 端口 {port} 无人监听")
            finally:
                sock.close()
    return "\n    ".join(parts)


def check_env(cookie: Dict[str, str], timeout: float, use_proxy: bool) -> int:
    """--check：把「为什么跑不通」一次性摊开。

    退出码（诊断语义，与签到不同）：0 = 自检跑完；1 = 网络/后端全不通；3 = cookie 无效。
    """
    print("=" * 60)
    print("贴吧签到自检")
    print("=" * 60)
    print(f"Python      : {sys.version.split()[0]} ({sys.executable})")
    print(f"OpenSSL     : {ssl.OPENSSL_VERSION}")
    print(f"requests    : {requests.__version__ if requests is not None else '未安装（将只用 urllib）'}")
    print(f"工作目录    : {SCRIPT_DIR}")
    print(f"代理配置    : {_describe_proxy()}")
    print(f"传输模式    : {'绕过代理直连' if not use_proxy else '使用系统代理'}")
    print()

    print("-" * 60)
    print("① 逐个后端探测贴吧连通性")
    print("-" * 60)
    names = (["requests", "urllib"] if requests is not None else ["urllib"])
    working: List[str] = []
    for name in names:
        try:
            backend = build_backend(name, cookie, min(timeout, 8.0), use_proxy)
        except RuntimeError as exc:
            print(f"  {name:9} 不可用: {exc}")
            continue
        ok, detail, elapsed = probe_backend(backend)
        print(f"  {name:9} {'✓ 通' if ok else '✗ 不通'}  {detail}  ({elapsed:.2f}s)")
        if ok:
            working.append(name)
    if not working:
        print()
        print("两个后端都连不上贴吧。这不是 cookie 的问题，先修网络：")
        print("  · 若日志里是 ReadTimeout 而 curl 能通：加 --use-proxy 或换个网络试试")
        print("  · 若走的是公司/校园网：可能需要代理才能出网")
        print("  · 用 curl 对照一下：curl -s -o NUL -w \"%{http_code}\" https://tieba.baidu.com/dc/common/tbs")
        return 1
    print()

    if not cookie.get("BDUSS"):
        print("没有提供 cookie，跳过登录态检查。")
        print("把 cookie 填进 config.json 或加 --cookie 后重跑 --check，才能确认账号可用。")
        print("=" * 60)
        return 0

    print("-" * 60)
    print("② 检查 cookie 登录态")
    print("-" * 60)
    backend = build_backend(working[0], cookie, timeout, use_proxy)
    client = TiebaClient(backend, cookie, retries=2, timeout=timeout)
    print(f"  BDUSS  长度 {len(cookie.get('BDUSS', ''))}"
          f"{'，STOKEN 已提供' if cookie.get('STOKEN') else '，没有 STOKEN（一般也行）'}")
    try:
        tbs = client.fetch_tbs()
        print(f"  ✓ 登录态有效，拿到 tbs = {tbs}")
    except RuntimeError as exc:
        print(f"  ✗ 登录态无效: {exc}")
        print()
        print("重新取 cookie 的方法：浏览器登录 tieba.baidu.com → F12 →")
        print("Application → Cookies → 复制 BDUSS 的值（必须有 BDUSS）。")
        print("=" * 60)
        return 3

    print()
    print("-" * 60)
    print("③ 读取关注列表")
    print("-" * 60)
    try:
        # 必须走和真正签到同一套取列表逻辑（移动端接口优先），
        # 否则自检会拿已经退化了的 /f/like 去报错，冤枉账号。
        forums, signed = client.fetch_forums_status()
    except RuntimeError as exc:
        print(f"  ✗ {exc}")
        print("=" * 60)
        return 3
    print(f"  ✓ 共关注 {len(forums)} 个吧")
    if signed:
        print(f"    其中 {len(signed)} 个今天已经签过：{', '.join(sorted(signed)[:5])}"
              f"{' ...' if len(signed) > 5 else ''}")
    for name in forums[:10]:
        print(f"      - {name}")
    if len(forums) > 10:
        print(f"      ... 其余 {len(forums) - 10} 个")
    print()
    print("-" * 60)
    print("④ 试签一个吧（真的会签到，可看到真实返回）")
    print("-" * 60)
    if not forums:
        print("  没有关注的吧，跳过。")
    else:
        item = client.sign_forum(forums[0], tbs)
        print(f"  {forums[0]} -> status={item.status} code={item.code} {item.message}")
    print()
    print("=" * 60)
    print("自检结论：环境与账号都能用，可以直接跑 python tieba_sign.py 了。")
    print("=" * 60)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="贴吧一键签到（使用 BDUSS 登录）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python tieba_sign.py --check\n"
            "  python tieba_sign.py\n"
            "  python tieba_sign.py --forums 李毅吧 原神吧\n"
            "  python tieba_sign.py --list\n"
            "  python tieba_sign.py --dry-run --json result.json\n"
        ),
    )
    parser.add_argument(
        "--cookie",
        help="整串 cookie（如 'BDUSS=xxx; STOKEN=yyy'）或裸 BDUSS；"
        "也可用环境变量 TIEBA_COOKIE",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help=f"配置文件路径（默认 {DEFAULT_CONFIG}）")
    parser.add_argument("--forums", nargs="+", metavar="吧名",
                        help="只签到指定的吧，不读关注列表")
    parser.add_argument("--all", action="store_true",
                        help="签到所有关注的吧（默认行为）")
    parser.add_argument("--list", action="store_true",
                        help="只列出所有关注的吧，不签到")
    parser.add_argument("--dry-run", action="store_true",
                        help="演练：只显示要签哪些吧，不发签到请求")
    parser.add_argument("--check", action="store_true",
                        help="自检：环境 / 代理 / HTTP 后端 / cookie 登录态 / 关注列表，并试签一个吧")
    parser.add_argument("--transport", choices=["auto", "requests", "urllib"],
                        default="auto",
                        help="HTTP 后端，默认 auto（先探测哪个通就用哪个）")
    parser.add_argument("--use-proxy", action="store_true",
                        help="使用系统代理（默认绕开直连；有些网络反而必须走代理）")
    parser.add_argument("--retry-failed", action="store_true",
                        help="对失败/异常的吧再补签一轮")
    parser.add_argument("--api", choices=["mobile", "web", "auto"], default="auto",
                        help="签到接口：auto（默认）优先走移动端接口，被拒签名时自动退回 "
                             "/sign/add；mobile 只用移动端；web 只用 /sign/add")
    parser.add_argument("--skip-signed", dest="skip_signed", action="store_true",
                        help="列表接口显示今天已签的吧直接跳过、不发请求（省流量，"
                             "但万一该标记不准会漏签一天）")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="每个吧之间的基础间隔秒数，默认 1.5")
    parser.add_argument("--retries", type=int, default=3,
                        help="网络请求重试次数，默认 3")
    parser.add_argument("--json", dest="json_out", type=Path,
                        help="把结果写入 JSON 文件")
    parser.add_argument("--log-path", dest="log_path", type=Path,
                        help="把本次运行追加到日志文件（计划任务用）")
    parser.add_argument("--timeout", type=float, default=15.0,
                        help="单次请求超时秒数，默认 15")
    return parser


def _resolve_out(path: Path) -> Path:
    return path if path.is_absolute() else SCRIPT_DIR / path


def _force_utf8_stdio() -> None:
    """Windows 控制台默认 GBK，会让中文输出变乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: Optional[Iterable[str]] = None) -> int:
    _force_utf8_stdio()
    started = time.time()
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    args.timeout = max(3.0, args.timeout)
    args.delay = max(0.0, args.delay)

    config = load_config(args.config)
    cookie = resolve_cookie(args, config)

    if args.check:
        return check_env(cookie, args.timeout, args.use_proxy)

    if not cookie.get("BDUSS"):
        print(
            "找不到 BDUSS。请用下面任一方式提供：\n"
            "  1) python tieba_sign.py --cookie \"BDUSS=xxxx; STOKEN=yyyy\"\n"
            "  2) 设置环境变量 TIEBA_COOKIE\n"
            f"  3) 在 {args.config} 里填 {{\"cookie\": \"BDUSS=xxxx; STOKEN=yyyy\"}}\n"
            f"  4) 把 cookie 导出到 {DEFAULT_COOKIE_FILE}（Netscape 格式）\n"
            "\n获取方法：浏览器登录 tieba.baidu.com -> F12 -> Application -> "
            "Cookies -> 复制 BDUSS 的值（登录必需）。\n"
            "填好后先跑一次自检：python tieba_sign.py --check",
            file=sys.stderr,
        )
        return 2
    if not cookie.get("STOKEN"):
        print("提示：cookie 里没有 STOKEN，仅用 BDUSS 一般也能签到。")

    try:
        backend, how = pick_backend(
            args.transport, cookie, args.timeout, args.use_proxy
        )
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    if args.transport == "auto":
        print(f"HTTP 后端：{how}")

    client = TiebaClient(
        backend,
        cookie,
        retries=args.retries,
        timeout=args.timeout,
        sign_mode=args.api,
    )

    # -- 决定要签哪些吧 -----------------------------------------------------
    if args.forums:
        forums = [f.strip() for f in args.forums if f.strip()]
        signed: set = set()
        print(f"目标：手动指定的 {len(forums)} 个吧")
    else:
        print("正在获取关注列表...")
        try:
            forums, signed = client.fetch_forums_status()
        except RuntimeError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        print(f"共关注 {len(forums)} 个吧")
        if signed:
            print(f"其中 {len(signed)} 个今天已经签过了")

    if not forums:
        print("没有需要签到的吧。")
        return 0

    if args.list:
        for name in forums:
            print(f"  {name}")
        return 0

    try:
        tbs = client.fetch_tbs()
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("dry-run：不会发送签到请求，将要签到的吧：")
        for name in forums:
            print(f"  - {name}")
        return 0

    print(f"开始签到（每次间隔约 {args.delay:.1f}s）...")
    skip = signed if (args.skip_signed and signed) else None
    if skip:
        print(f"已签过的 {len(skip)} 个吧按列表接口结果直接跳过，不再发请求")
    stats = sign_forums(client, forums, args.delay, tbs, skip_signed=skip)

    if client.mode_switched:
        code, msg = client.mode_switched
        print()
        print(f"  ! 移动端接口拒绝了签名（code={code} {msg}），本轮已改用 Web 接口 /sign/add。")
        print("    这通常意味着移动端签名算法变了，可以去看看脚本是否有更新。")

    if args.retry_failed:
        bad = [i.forum for i in stats.items if i.status in ("failed", "error")]
        if bad:
            print(f"\n对 {len(bad)} 个失败的吧补签一轮...")
            time.sleep(5)
            retry_stats = sign_forums(client, bad, args.delay, tbs)
            done = {i.forum for i in retry_stats.items}
            stats.items = [i for i in stats.items if i.forum not in done]
            stats.items.extend(retry_stats.items)
            stats.ok += retry_stats.ok
            stats.already += retry_stats.already
            stats.failed = retry_stats.failed
            stats.error = retry_stats.error
            stats.missed = retry_stats.missed

    print_summary(stats)

    if args.json_out:
        path = _resolve_out(args.json_out)
        try:
            write_json(path, stats, started, args.dry_run)
        except OSError as exc:
            print(f"写入 {path} 失败: {exc}", file=sys.stderr)

    if args.log_path:
        path = _resolve_out(args.log_path)
        try:
            write_log(path, stats, started)
        except OSError as exc:
            print(f"写入日志 {path} 失败: {exc}", file=sys.stderr)

    return 0 if (stats.failed == 0 and stats.error == 0) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        sys.exit(130)
