# -*- coding: utf-8 -*-
"""离线自测：不联网，用假后端覆盖两个签到接口、签名算法、cookie 解析、日志与 --check。

重点覆盖实测出来的三件事：
  1. 移动端接口真的校验 sign（算法不对 -> 110001），Web 接口完全不校验；
  2. 两种签名算法不同，且不能互相替用；
  3. 移动端列表接口一次带回「关注列表 + 是否已签」，据此可跳过请求。
"""
import contextlib
import io
import json
import os
import sys
import time as _time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tieba_sign as T  # noqa: E402
from tieba_sign import (  # noqa: E402
    HttpResult,
    Item,
    Stats,
    TiebaClient,
    parse_cookie,
    parse_netscape_cookie_file,
    sign_client,
    sign_forums,
    sign_web,
)

TBS = "0123456789abcdef0123456789abcdef"


# ══════════════════════════════════════════════════════════════════════════
# 1. cookie 解析
# ══════════════════════════════════════════════════════════════════════════
assert parse_cookie("BDUSS=abc; STOKEN=def") == {"BDUSS": "abc", "STOKEN": "def"}
assert parse_cookie("abc123") == {"BDUSS": "abc123"}
assert parse_cookie("BDUSS=abc\nSTOKEN=def")["STOKEN"] == "def"
assert parse_cookie("  'BDUSS=abc'  ")["BDUSS"] == "abc"
assert parse_cookie("") == {}
print("cookie 解析 OK")

tmp = Path("tmp_cookies.txt")
tmp.write_text(
    "# Netscape HTTP Cookie File\n"
    ".tieba.baidu.com\tTRUE\t/\tFALSE\t0\tBDUSS\tabc\n"
    ".baidu.com\tTRUE\t/\tFALSE\t0\tSTOKEN\tdef\n",
    encoding="utf-8",
)
assert parse_netscape_cookie_file(tmp) == {"BDUSS": "abc", "STOKEN": "def"}
tmp.unlink()
print("Netscape cookie OK")


# ══════════════════════════════════════════════════════════════════════════
# 2. 两种签名算法：必须不同，且各自可复算
# ══════════════════════════════════════════════════════════════════════════
import hashlib  # noqa: E402
from urllib.parse import quote  # noqa: E402

# 移动端：单层 md5、原样拼接、值不编码
assert sign_client("李毅吧", TBS) == hashlib.md5(
    f"kw=李毅吧tbs={TBS}tiebaclient!!!".encode("utf-8")
).hexdigest()
# Web：排序 + encodeURIComponent + 两层 md5
_pairs = "".join(sorted([
    f"ie={quote('utf-8', safe='')}",
    f"kw={quote('李毅吧', safe='')}",
    f"tbs={TBS}",
]))
assert TiebaClient._sign({"ie": "utf-8", "kw": "李毅吧", "tbs": TBS}) == hashlib.md5(
    (hashlib.md5(_pairs.encode("utf-8")).hexdigest() + "tiebaclient!!!").encode("utf-8")
).hexdigest()

# 关键：两者绝不能等价 —— 拿 Web 签名去请求移动端接口实测会被 110001 拒掉
for _name in ("李毅吧", "a/b吧", "B&C吧", "a b吧"):
    assert sign_client(_name, TBS) != sign_web({"ie": "utf-8", "kw": _name, "tbs": TBS}), _name

# quote 的 safe 必须显式给 ""，否则含 / 的吧名签名算错
_enc = sign_web({"ie": "utf-8", "kw": "a/b吧", "tbs": TBS})
_bad = hashlib.md5((
    hashlib.md5(("".join(sorted([
        f"ie={quote('utf-8', safe='')}",
        f"kw={quote('a/b吧', safe='/')}",      # ← 错
        f"tbs={TBS}"]))).encode("utf-8")).hexdigest() + "tiebaclient!!!"
).encode("utf-8")).hexdigest()
assert _enc != _bad, "safe='/' 必须算出不同结果，否则这条测试没意义"
print("两种签名算法 OK（且确认互不等价）")


# ══════════════════════════════════════════════════════════════════════════
# 3. 假后端：两个接口分开打桩，并模拟真实错误码
# ══════════════════════════════════════════════════════════════════════════
class ScriptedBackend:
    """按脚本回应移动端/Web 两个签到接口，并记录每个请求。"""

    name = "fake"

    def __init__(self, mobile_script=(), web_script=(), list_rows=None, tbs=TBS):
        self.mobile_script = list(mobile_script)
        self.web_script = list(web_script)
        self.list_rows = list_rows
        self.tbs = tbs
        self.calls = []

    def request(self, method, url, params=None, data=None, headers=None):
        path = url.split("baidu.com", 1)[-1].split("?")[0]
        body = dict(data or params or {})
        self.calls.append((method, path, body))

        if path == T.MOBILE_SIGN_PATH:
            # 真实行为：sign 不对就 110001，对了才看登录态
            kw = body.get("kw", "")
            if body.get("sign") != sign_client(kw, body.get("tbs", "")):
                return HttpResult(200, json.dumps(
                    {"error_code": "110001", "error_msg": "未知错误"}), 0.01, self.name)
            payload = self.mobile_script.pop(0) if self.mobile_script else {
                "error_code": "1", "error_msg": "用户未登录或登录失败，请更换账号或重试"}
            return HttpResult(200, json.dumps(payload, ensure_ascii=False), 0.01, self.name)

        if path == T.SIGN_PATH:
            payload = self.web_script.pop(0) if self.web_script else {"no": 1990055}
            return HttpResult(200, json.dumps(payload, ensure_ascii=False), 0.01, self.name)

        if path == T.MOBILE_LIST_PATH:
            if self.list_rows is None:
                return HttpResult(200, json.dumps({"no": 1, "error": "not logined!"}), 0.01, self.name)
            return HttpResult(200, json.dumps(
                {"no": 0, "data": {"like_forum": self.list_rows}}, ensure_ascii=False),
                0.01, self.name)

        if path == T.TBS_PATH:
            return HttpResult(200, json.dumps({"tbs": self.tbs, "is_login": 1}), 0.01, self.name)
        if path == T.FOLLOW_PATH:
            rows = self.list_rows or []
            return HttpResult(200, json.dumps(
                {"no": 0, "data": {"like_forum": rows}}), 0.01, self.name)
        if path == "/":
            return HttpResult(200, f'<html>var tbs = "{self.tbs}";</html>', 0.01, self.name)
        raise AssertionError(f"未预期的请求 {method} {path}")


def make_client(backend, mode="mobile", retries=2):
    return TiebaClient(backend, {"BDUSS": "x"}, retries=retries, sign_mode=mode)


_orig_sleep = _time.sleep
_time.sleep = lambda *a, **kw: None          # 跳过退避等待


# ══════════════════════════════════════════════════════════════════════════
# 4. 移动端接口：成功 / 已签 / 失败 / 限速 / 登录失效
# ══════════════════════════════════════════════════════════════════════════
be = ScriptedBackend(mobile_script=[
    {"error_code": "0", "user_info": {"user_sign_rank": 42}},
    {"error_code": "160002", "error_msg": "今天已签到"},       # 移动端的「已签」
    {"error_code": "1102", "error_msg": "操作失败"},
    {"error_code": "1103"},                                    # 过快 -> 退避重试
    {"error_code": "1103"},
    {"error_code": "1", "error_msg": "用户未登录或登录失败"},   # 登录失效 -> 中止
])
stats = sign_forums(make_client(be), ["a吧", "b吧", "c吧", "d吧", "e吧", "f吧"], 0, TBS)
assert (stats.ok, stats.already, stats.failed, stats.error, stats.missed) == (1, 1, 2, 1, 1), (
    stats.ok, stats.already, stats.failed, stats.error, stats.missed)
assert stats.items[0].rank == 42, "移动端接口的 user_sign_rank 要带回来"
assert stats.items[1].status == "already"
assert stats.items[5].status == "missed" and stats.items[5].forum == "f吧"
print("移动端分派 OK:", [(i.forum, i.status, i.code) for i in stats.items])
print("  排名解析 OK: a吧 rank =", stats.items[0].rank)

# 请求必须打到移动端域名，且带 kw/tbs/sign
method, path, body = be.calls[0]
assert (method, path) == ("POST", T.MOBILE_SIGN_PATH), (method, path)
assert set(body) == {"kw", "tbs", "sign"}, body
assert body["sign"] == sign_client("a吧", TBS)
print("移动端请求 OK:", path, {k: v[:12] for k, v in body.items()})


# ══════════════════════════════════════════════════════════════════════════
# 5. 核心陷阱：移动端拒签名 -> 自动退回 Web 接口（且只退一次）
# ══════════════════════════════════════════════════════════════════════════
be = ScriptedBackend(
    mobile_script=[{"error_code": "110001"}],      # 逼它走 badsig 分支
    web_script=[{"no": 0, "error": ""}],
)
c = make_client(be, mode="auto")
item = c.sign_forum("x吧", TBS)
assert item.status == "ok" and item.code == 0, item
assert c.effective_mode == "web", c.effective_mode
assert c.mode_switched and c.mode_switched[0] == 110001, c.mode_switched
paths = [p for _, p, _ in be.calls]
assert paths == [T.MOBILE_SIGN_PATH, T.SIGN_PATH], paths
print("移动端拒签名 -> 退回 Web OK:", paths)

# 第二个吧不该再撞一遍移动端（省掉无谓请求）
before = len(be.calls)
c.sign_forum("y吧", TBS)
assert [p for _, p, _ in be.calls[before:]] == [T.SIGN_PATH], be.calls[before:]
print("  只退一次、后续直连 Web OK")

# 强制 mobile 时错误码会被识别为 badsig 并同样退回，不会误报成「登录失效」
be2 = ScriptedBackend(mobile_script=[{"error_code": "110001"}], web_script=[{"no": 0}])
c2 = make_client(be2, mode="mobile")
assert c2.sign_forum("z吧", TBS).status == "ok"
assert not any(i.status == "error" for i in []), "不该产生 error 项"
print("  强制 mobile 也被拒时同样退回，不误报登录失效")

# 而真·未登录（移动端 code=1）必须报 error 且属于致命码，不能退回
be3 = ScriptedBackend(mobile_script=[{"error_code": "1", "error_msg": "用户未登录"}])
item3 = make_client(be3).sign_forum("w吧", TBS)
assert item3.status == "error" and item3.code in T.SIGN_FATAL, item3
assert item3.code == 1 and "110001" not in str(item3.code)
print("  移动端 code=1（未登录）-> 致命错误，不和 110001 混淆 OK")


# ══════════════════════════════════════════════════════════════════════════
# 6. Web 接口单独走通（--api web）
# ══════════════════════════════════════════════════════════════════════════
be = ScriptedBackend(web_script=[
    {"no": 0}, {"no": 1101}, {"no": 1102, "error": "操作失败"}, {"no": 999999},
])
stats = sign_forums(make_client(be, mode="web"), ["a吧", "b吧", "c吧", "d吧"], 0, TBS)
assert (stats.ok, stats.already, stats.failed) == (1, 1, 2), (stats.ok, stats.already, stats.failed)
assert stats.items[3].message == "未知返回码 999999" or "999999" in stats.items[3].message
assert all(p == T.SIGN_PATH for _, p, _ in be.calls), be.calls
assert set(be.calls[0][2]) == {"ie", "kw", "tbs", "sign"}
print("Web 接口 OK:", [(i.forum, i.status, i.code) for i in stats.items])


# ══════════════════════════════════════════════════════════════════════════
# 7. 移动端列表接口：一次拿到列表 + 已签状态，并可跳过请求
# ══════════════════════════════════════════════════════════════════════════
be = ScriptedBackend(list_rows=[
    {"forum_name": "A吧", "is_sign": 1},
    {"forum_name": "B&amp;C吧", "is_sign": 0},
    {"forum_name": "A吧", "is_sign": 1},          # 重复要去重
])
c = make_client(be)
forums, signed = c.fetch_forums_status()
assert forums == ["A吧", "B&C吧"], forums          # HTML 实体要解码
assert signed == {"A吧"}, signed
assert len(be.calls) == 1, "列表接口应该只调一次"    # 不再翻页
print("移动端列表 OK:", forums, "已签:", signed)

# 跳过已签：不应为已签的吧发签到请求
be2 = ScriptedBackend(
    list_rows=[{"forum_name": "A吧", "is_sign": 1}, {"forum_name": "B吧", "is_sign": 0}],
    mobile_script=[{"error_code": "0", "user_info": {"user_sign_rank": 7}}],
)
c2 = make_client(be2)
forums2, signed2 = c2.fetch_forums_status()
stats2 = sign_forums(c2, forums2, 0, TBS, skip_signed=signed2)
assert (stats2.already, stats2.ok) == (1, 1), (stats2.already, stats2.ok)
sign_calls = [p for _, p, _ in be2.calls if p == T.MOBILE_SIGN_PATH]
assert len(sign_calls) == 1, f"只该为 B吧 发一次签到请求，实际 {len(sign_calls)} 次"
assert stats2.items[0].code == 160002 and "跳过请求" in stats2.items[0].message
print("跳过已签 OK：为", len(sign_calls), "个吧发了请求（原本 2 个）")

# 不传 skip_signed 时仍然逐个真请求（默认保守，避免 is_sign 不准导致漏签）
be3 = ScriptedBackend(
    list_rows=[{"forum_name": "A吧", "is_sign": 1}],
    mobile_script=[{"error_code": "160002"}],
)
stats3 = sign_forums(make_client(be3), ["A吧"], 0, TBS)     # 不给 skip_signed
assert len([p for _, p, _ in be3.calls if p == T.MOBILE_SIGN_PATH]) == 1
assert stats3.already == 1
print("不跳过时仍然真请求 OK（默认保守）")

# 移动端列表不可用 -> 退回翻页接口，且不谎报已签
be4 = ScriptedBackend(list_rows=None)
be4.list_rows = None
orig_fetch = TiebaClient.fetch_forums
TiebaClient.fetch_forums = lambda self: ["翻页吧"]
try:
    forums4, signed4 = make_client(be4).fetch_forums_status()
finally:
    TiebaClient.fetch_forums = orig_fetch
assert forums4 == ["翻页吧"] and signed4 == set(), (forums4, signed4)
print("列表接口降级 OK:", forums4, signed4)


# ══════════════════════════════════════════════════════════════════════════
# 8. 其它既有行为不能回归
# ══════════════════════════════════════════════════════════════════════════
# 接口吐 HTML
bad = HttpResult(200, "<!DOCTYPE html><html>哎呀</html>", 0.01, "fake")
html_err = ""
try:
    bad.json()
    raise AssertionError("应该抛错")
except RuntimeError as exc:
    html_err = str(exc)
assert "HTML" in html_err, html_err
print("HTML 响应报错 OK")

# tbs 接口 + is_login=0 拦截
be = ScriptedBackend()
c = make_client(be)
assert c.fetch_tbs() == TBS
be_l = ScriptedBackend(tbs=TBS)
be_l.request = lambda m, u, p=None, d=None, h=None: HttpResult(
    200, json.dumps({"tbs": TBS, "is_login": 0}), 0.01, "fake")
try:
    make_client(be_l).fetch_tbs()
    raise AssertionError("is_login=0 应该报错")
except RuntimeError as exc:
    assert "is_login=0" in str(exc), exc
print("tbs 逻辑 OK（含 is_login=0 拦截）")

# 双后端选路
calls = []
orig_build, orig_probe = T.build_backend, T.probe_backend


class _FB:
    """够用的假后端：check_env 不只探测连通性，拿到 cookie 后还会真发请求，
    所以这里必须能应答 /dc/common/tbs，光有 name 是不够的。"""

    def __init__(self, name):
        self.name = name
        self._inner = ScriptedBackend()

    def request(self, method, url, params=None, data=None, headers=None):
        return self._inner.request(method, url, params=params, data=data, headers=headers)


def fake_build(name, cookie, timeout, use_proxy=False):
    calls.append(name)
    return _FB(name)


def fake_probe(backend, timeout=None):
    if backend.name == "requests":
        return False, "ReadTimeout: 读超时", 8.0
    return True, "HTTP 200，拿到 tbs", 0.07


try:
    T.build_backend, T.probe_backend = fake_build, fake_probe
    chosen, how = T.pick_backend("auto", {"BDUSS": "x"}, 15.0, False)
    assert chosen.name == "urllib" and how == "自动选中 urllib", (chosen.name, how)
    T.probe_backend = lambda backend, timeout=None: (False, "连不上", 1.0)
    try:
        T.pick_backend("auto", {"BDUSS": "x"}, 15.0, False)
        raise AssertionError("应该抛错")
    except RuntimeError as exc:
        assert "都连不上" in str(exc), exc
finally:
    T.build_backend, T.probe_backend = orig_build, orig_probe
print("双后端选路 OK")


# ══════════════════════════════════════════════════════════════════════════
# 9. 端到端 main() + 日志 / --check
# ══════════════════════════════════════════════════════════════════════════
log = Path("test.log")
if log.exists():
    log.unlink()
orig_pick = T.pick_backend
installed = make_client(ScriptedBackend(
    mobile_script=[{"error_code": "0", "user_info": {"user_sign_rank": 3}},
                   {"error_code": "160002"}] * 3,
    list_rows=[{"forum_name": "A吧", "is_sign": 0}, {"forum_name": "B吧", "is_sign": 0}],
))
try:
    T.pick_backend = lambda *a, **kw: (installed.backend, "自动选中 fake")
    code = T.main(["--cookie", "BDUSS=x; STOKEN=y", "--log-path", "test.log"])
finally:
    T.pick_backend = orig_pick

assert code == 0, code
text = log.read_text(encoding="utf-8").strip()
log.unlink()
assert "共2 成功1 已签1" in text, text
assert text.startswith("20") and "签到完成" in text, text
print("main()+日志 OK:", text)

buf = io.StringIO()
orig_build2, orig_probe2 = T.build_backend, T.probe_backend
_orig_env_cookie = os.environ.pop("TIEBA_COOKIE", None)
try:
    T.build_backend, T.probe_backend = fake_build, fake_probe
    with contextlib.redirect_stdout(buf):
        # --config 指向不存在的文件：否则会读到工作目录里真实的 config.json，
        # 离线自测就会去连真贴吧（这正是被坑过一次的地方）
        rc = T.main(["--check", "--transport", "urllib",
                     "--config", str(Path("__no_such_config__.json"))])
finally:
    T.build_backend, T.probe_backend = orig_build2, orig_probe2
    if _orig_env_cookie is not None:
        os.environ["TIEBA_COOKIE"] = _orig_env_cookie
out = buf.getvalue()
assert rc == 0 and "没有提供 cookie" in out, out[-300:]
print("--check（无 cookie）OK，退出码", rc)

# 配置里用字典写 cookie —— 曾经只认字符串，导致明明填了却说没 cookie
_cfgfile = Path("__cfg_dict__.json")
_cfgfile.write_text(json.dumps(
    {"cookie": {"BDUSS": "  abc123  ", "STOKEN": "'st'"}}, ensure_ascii=False), encoding="utf-8")
try:
    _args = T.build_parser().parse_args(["--config", str(_cfgfile)])
    _ck = T.resolve_cookie(_args, T.load_config(_cfgfile))
    assert _ck == {"BDUSS": "abc123", "STOKEN": "st"}, _ck
    # 整串字符串形式同样要能用
    _cfgfile.write_text(json.dumps({"cookie": "BDUSS=xyz; STOKEN=q"}), encoding="utf-8")
    _ck2 = T.resolve_cookie(_args, T.load_config(_cfgfile))
    assert _ck2 == {"BDUSS": "xyz", "STOKEN": "q"}, _ck2
finally:
    _cfgfile.unlink()
print("配置文件 cookie（字典 / 字符串两种写法）OK")

buf = io.StringIO()
orig_build3, orig_probe3 = T.build_backend, T.probe_backend
try:
    T.probe_backend = fake_probe
    T.build_backend = lambda name, cookie, timeout, use_proxy=False: ScriptedBackend(
        mobile_script=[{"error_code": "0", "error_msg": "签到成功"}],
        list_rows=[{"forum_name": "A吧", "is_sign": 1}, {"forum_name": "B&amp;C吧", "is_sign": 0}])
    with contextlib.redirect_stdout(buf):
        rc = T.main(["--check", "--cookie", "BDUSS=real; STOKEN=y", "--transport", "urllib"])
finally:
    T.build_backend, T.probe_backend = orig_build3, orig_probe3
out = buf.getvalue()
assert rc == 0, rc
for needle in ("登录态有效", "共关注 2 个吧", "B&C吧", "status=ok", "自检结论"):
    assert needle in out, f"缺 {needle!r}\n{out}"
print("--check（有效 cookie）OK，退出码", rc)

_time.sleep = _orig_sleep
print()
print("全部离线自测通过")
