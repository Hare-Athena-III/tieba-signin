# -*- coding: utf-8 -*-
"""把 Web 签名与客户端签名的 preimage 并排摊开，顺便确认有没有更省的调用方式。"""
import hashlib
from urllib.parse import quote

from tieba_sign import TiebaClient

SALT = "tiebaclient!!!"
TBS = "0123456789abcdef0123456789abcdef"
NAME = "a/b吧"


def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


params = {"ie": "utf-8", "kw": NAME, "tbs": TBS}
enc = {k: quote(str(v), safe="") for k, v in params.items()}

print("① Web 版 preimage 的两种拼法，看哪个能复现脚本里的 _sign：")
for label, joined in (
    ("join 无分隔符", "".join(sorted(f"{k}={v}" for k, v in enc.items()))),
    ("join & 分隔 ", "&".join(sorted(f"{k}={v}" for k, v in enc.items()))),
):
    first = md5(joined)
    sign = md5(first + SALT)
    ok = sign == TiebaClient._sign(params)
    print(f"   {label}  sign={sign}  {'← 就是这个' if ok else ''}")

print()
print("② 客户端版（油猴脚本）preimage —— 注意参数名都在，但值不编码：")
raw = f"kw={NAME}tbs={TBS}"
print(f"   preimage = {raw}{SALT}")
print(f"   sign     = {md5(raw + SALT)}")

print()
print("③ 两边 preimage 的结构差异：")
sorted_pairs = sorted(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
print(f"   Web : {' + '.join(sorted_pairs)}   （k=v 对，值编码，排序）")
print(f"   客户端: kw={NAME}  tbs={TBS}            （只有两个参数，值不编码）")

print()
print("④ 顺带确认：quote 的 safe 参数确实会改变签名")
print(f"   safe=''  -> {md5(md5('kw=' + quote(NAME, safe='') + '&tbs=' + TBS) + SALT)}")
print(f"   safe='/' -> {md5(md5('kw=' + quote(NAME, safe='/') + '&tbs=' + TBS) + SALT)}")
