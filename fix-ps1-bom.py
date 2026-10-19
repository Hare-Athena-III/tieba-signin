# -*- coding: utf-8 -*-
"""给 .ps1 补 UTF-8 BOM —— 编辑完 PowerShell 脚本后跑一下。

为什么需要：PowerShell 5.1 按 ANSI（中文机器是 GBK）解析没有 BOM 的脚本，
代码位置里的中文会变成乱码并破坏语法，而且**报错信息指不到真正的原因**
（典型症状：脚本静默退出 0 却毫无输出）。

用法：
    python -X utf8 fix-ps1-bom.py            # 检查并修复 scripts/*.ps1
    python -X utf8 fix-ps1-bom.py --check    # 只检查，不修改（有缺就退出码 1）
"""
import sys
from pathlib import Path

BOM = b"\xef\xbb\xbf"
SCRIPT_DIR = Path(__file__).resolve().parent
check_only = "--check" in sys.argv

targets = sorted(SCRIPT_DIR.glob("*.ps1")) + sorted(SCRIPT_DIR.glob("scripts/*.ps1"))
if not targets:
    print("没找到任何 .ps1")
    sys.exit(0)

missing = []
for p in targets:
    raw = p.read_bytes()
    if raw.startswith(BOM):
        print(f"  OK      {p.relative_to(SCRIPT_DIR)}")
        continue
    # 先确认它确实是 UTF-8，避免把别的编码搞坏
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        print(f"  跳过    {p.relative_to(SCRIPT_DIR)}  不是 UTF-8：{exc}")
        continue
    missing.append(p)
    if check_only:
        print(f"  缺 BOM  {p.relative_to(SCRIPT_DIR)}")
    else:
        p.write_bytes(BOM + raw)
        print(f"  已修复  {p.relative_to(SCRIPT_DIR)}")

if missing and check_only:
    print(f"\n{len(missing)} 个脚本缺 UTF-8 BOM。去掉 --check 重新运行即可修复。")
    sys.exit(1)
if missing:
    print(f"\n修好了 {len(missing)} 个。")
else:
    print("\n所有 .ps1 都带 UTF-8 BOM。")
