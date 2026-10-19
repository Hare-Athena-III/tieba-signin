# 贴吧一键签到

用 BDUSS 登录，自动取出「我关注的吧」并逐个签到。纯 Python，不碰浏览器数据，不依赖第三方工具。

- **零强依赖**：装了 `requests` 就用它，没装（或它打不通）就自动切到标准库 `urllib`。
- **能无人值守**：提供 Windows 计划任务一键注册，开机自动跑、每天定时跑。
- **出问题能自己说清楚**：`--check` 把「网络 / 登录态 / 关注列表 / 试签」四步摊开，直接告诉你卡在哪。

## 文件

| 文件 | 说明 |
| --- | --- |
| `tieba_sign.py` | 主脚本 |
| `selftest.py` | 离线自测：cookie 解析、返回码分支、登录失效中断、签名格式、日志写入，全程不联网 |
| `install-task.cmd` | **双击即可**注册计划任务（自动弹 UAC 提权，装完还会试跑一次） |
| `scripts/install-task.ps1` | 注册计划任务的 PowerShell 实现，参数更多 |
| `scripts/run-sign.ps1` | 计划任务实际调用的包装脚本：等网络 + 写日志 + 日志轮转 |
| `scripts/status-task.ps1` | 查看任务状态、下次运行时间、日志尾部 |
| `scripts/uninstall-task.ps1` | 卸载计划任务 |
| `config.example.json` | 配置模板，复制成 `config.json` 填 cookie |
| `sig-preimage.py` | 开发用：把签名原文打印出来，方便和浏览器里的实现对拍 |

`config.json`（含 cookie）、`cookies.txt`、`sign.log`、`result.json`、`task-config.json` 都写进了 `.gitignore`，不会被提交。

```powershell
python selftest.py    # 全部通过会打印「全部离线自测通过」
```

## 安装

非必需：

```powershell
pip install requests
```

## 配置 cookie（4 选 1）

**怎么拿 BDUSS**：浏览器登录 <https://tieba.baidu.com> → `F12` → **Application / 存储** → **Cookies** → `https://tieba.baidu.com` → 找到 `BDUSS`，复制它的**值**（`STOKEN` 可选，建议一起带上）。

```powershell
# 1) 命令行直接给（最省事，但会进 PowerShell 历史）
python tieba_sign.py --cookie "BDUSS=你的一长串; STOKEN=你的STOKEN"

# 2) 环境变量（当前会话有效）
$env:TIEBA_COOKIE = "BDUSS=你的一长串; STOKEN=你的STOKEN"
python tieba_sign.py

# 3) 配置文件（推荐，长期用）
Copy-Item config.example.json config.json
notepad config.json      # 把 cookie 那行的值换成自己的

# 4) Netscape 格式
curl -c cookies.txt ...   # 或用浏览器插件导出到本目录 cookies.txt
```

## 常用命令

```powershell
python tieba_sign.py --check          # 自检：环境 + 网络 + cookie + 试签，出问题先跑这个
python tieba_sign.py                  # 签到所有关注的吧
python tieba_sign.py --list           # 只列出关注的吧，不签到
python tieba_sign.py --dry-run        # 演练：只看会签哪些吧
python tieba_sign.py --forums 甲吧 乙吧   # 只签指定吧
python tieba_sign.py --retry-failed   # 失败的吧隔 5 秒补签一轮
python tieba_sign.py --delay 3 --json result.json
```

常用参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--check` | — | 自检四个步骤（连通性 / 登录态 / 关注列表 / 试签一个吧），发现问题直接告诉你卡在哪 |
| `--cookie` | — | 整串 cookie 或裸 BDUSS |
| `--config` | `config.json` | 配置文件路径 |
| `--forums` | — | 只签指定吧（覆盖关注列表） |
| `--list` / `--dry-run` | — | 只列吧 / 不真正签到 |
| `--retry-failed` | — | 补签失败项 |
| `--delay` | `1.5` | 每个吧之间的基础间隔（秒），实际还会加 0~40% 随机抖动 |
| `--retries` | `3` | 网络错误/5xx 的重试次数 |
| `--timeout` | `15` | 单请求超时（秒） |
| `--transport` | `auto` | HTTP 后端：`auto` 先试 requests 再退到 urllib，也可强制 `requests` / `urllib` |
| `--api` | `auto` | 签到接口：`auto` 先用移动端接口、被拒签名时自动退回 `/sign/add`；也可强制 `mobile` / `web` |
| `--skip-signed` | 关 | 列表接口显示今天已签的吧**直接跳过、不发请求**（省流量，但见下方提醒） |
| `--use-proxy` | 关 | 走系统代理。默认**绕开**代理直连，原因见下面「代理」一节 |
| `--json` | — | 结果写入 JSON 文件 |
| `--log-path` | — | 追加一行汇总到日志文件（给计划任务用） |

退出码：`0` 全部成功，`1` 有失败/异常，`2` 没找到 cookie，`3` cookie 无效/登录态失效。

> 注意 `--check` 的退出码语义不同：它是**诊断**，`0` 表示自检跑完了；网络不通返回 `1`，cookie 无效返回 `3`。所以别把 `--check` 配进计划任务。

> 关于 `--skip-signed`：它靠列表接口的 `is_sign` 字段跳过已签的吧。好处是关注的吧很多时（50+ 个）能省掉一大批请求、也不容易触发限流；风险是这个字段万一不准就会漏签一天。所以**默认不开**——签到本来就是幂等的，多打一次请求最多得到「今天已签到」，但漏签就补不回来了。

## 代理：为什么默认要绕开

装了代理软件（Clash 之类）时，系统代理会让请求走代理，而贴吧对代理出口 IP 很敏感——轻则超时，重则触发风控。所以脚本默认**绕开代理直连**，需要走代理时显式加 `--use-proxy`。

反过来，如果你所在网络（公司/校园网）必须过代理才能上网，那就加上 `--use-proxy`。

## 实现要点

- **登录**：整串 cookie 塞进会话（含 `BDUSS`），跟浏览器登录等价。
- **传输层**：有 `requests` 就先用 `requests`，打不通自动切到标准库 `urllib`（同一套请求/解析逻辑，`--transport` 可强制）。这是实机踩出来的：某些环境下 `requests` 打 `tieba.baidu.com` 稳定 `ReadTimeout`，而 `urllib` 0.05s 就通——所以**没装 `requests` 也能跑**。
- **关注列表**：优先 `GET /mo/q/newmoindex`（移动端接口），**一次**调用就返回关注列表，而且每个吧带 `is_sign` 标注今天签没签；它不可用时退回 `GET /f/like?pn=N` 翻页（每页 50 条）。两条路都会做 HTML 反转义和去重。
- **tbs**：优先用 cookie 里的 `tbs`；否则请求专用接口 `GET /dc/common/tbs`——它带 `is_login` 字段，能直接分清「没登录」和「拿到了 tbs」（`is_login=0` 立刻报 cookie 失效，不再含糊）；接口异常才退回首页 HTML 正则抓 20 位以上十六进制串。
- **签到接口**：默认走移动端 `POST c.tieba.baidu.com/c/c/forum/sign`，`kw` / `tbs` / `sign` 三个参数；被拒签名时自动退回 Web 的 `POST /sign/add`。两条路的差别实测如下：

  | | Web `/sign/add` | 移动端 `/c/c/forum/sign` |
  | --- | --- | --- |
  | 校验 `sign` 吗 | **不校验**（写错、甚至不传，返回完全一样） | **校验**（算错就 `110001`） |
  | 签名算法 | 排序 + `encodeURIComponent` + 两层 md5 | 单层 md5、原样拼接、值**不编码** |
  | 「已签到」返回码 | `1101` | `160002` |
  | 额外信息 | 无 | `user_info.user_sign_rank`（本吧排名） |

  两种签名唯一的共同点是固定盐 `tiebaclient!!!`，**不能互相替用**——拿 Web 签名去请求移动端接口会被直接拒掉（已实测）。
- **签名**：`sign_client(kw, tbs) = md5(f"kw={kw}tbs={tbs}tiebaclient!!!")`（移动端）；`sign_web(params) = md5(sorted(k=v 对，值 encodeURIComponent) 拼接后 md5 + 盐)`（Web）。
  Python 的 `quote` 必须显式 `safe=''` 才等价于 JS 的 `encodeURIComponent`（默认 `safe='/'` 会把 `/` 放过去，导致签名不一致）。该算法已与 Node 的 `crypto` 实现逐位比对通过。
- **返回码**：`0` 成功、`1101`/`160002` 今天已签到（都算成功）；`1103/1104` 签到过快，等 4~6 秒重试一次；`1/2/4/5/7/10/340006` 视为登录失效，立刻中止并把剩余吧标记为跳过，不再空跑；`110001` 是移动端「签名被拒」，会被当作接口层问题、退回 Web 接口而不是报成登录失效；其余未知码算失败并原样报出（`1990055` 会明确说「账号未实名」，别再去折腾 cookie）。
- **响应兜底**：接口偶尔吐 HTML（验证码/风控页）而不是 JSON，这时给一句「返回的是 HTML，通常是触发验证码或 cookie 失效」，而不是裸的 `JSONDecodeError`。
- **错误信息**：所有失败都带「下一步该做什么」。`--check` 把这些探测摊开成四步，是排障入口。
- **编码**：Windows 控制台默认 GBK，启动时把 stdout/stderr 强制为 UTF-8，否则中文全乱码。另外 PowerShell 5.1 会按 ANSI 读取 `.ps1`，所以 `scripts/*.ps1` 一律保存为 **UTF-8 带 BOM**（不带 BOM 的话，参数名紧跟中文标点时会被解析成 `${code，}` 而报错）。
- **自动运行的日志**：计划任务在 0 号会话里跑，控制台输出没人看见，所以 `tieba_sign.py --log-path` 自己追加一行汇总；包装脚本再把 python 的 stderr 原文也抄进同一个日志，并对超过 1 MB 的日志做轮转。包装脚本还会临时把 `$ErrorActionPreference` 降成 `Continue`——否则 python 往 stderr 写一行字就会被当成终止性错误，把整个包装脚本打断。

## 自动签到（Windows 计划任务）

用计划任务而不是常驻进程：不占前台窗口、开机后按点自己跑、崩了也不影响下次。

**最省事的方式：双击 `install-task.cmd`。** 它会自己弹 UAC 提权，注册「每天 11:00 + 开机时」两个触发，装完立刻试跑一次并把结果打给你看。上面的时间、参数都能在文件里改。

想在命令行里精细控制：

```powershell
# 0) 先把 cookie 填好（config.json），确保手动跑一次是成功的
python tieba_sign.py

# 1) 先预览会注册成什么（不写任何东西）
powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1 -Time 11:00 -AtStartup -WhatIf

# 2) 真注册（需要管理员身份）：每天 11:00 + 开机自动跑
powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1 -Time 11:00 -AtStartup -NetworkWait 120

# 3) 马上试跑一次，看日志
Start-ScheduledTask -TaskName TiebaSign
powershell -ExecutionPolicy Bypass -File scripts\status-task.ps1
```

| 参数 | 说明 |
| --- | --- |
| `-Time HH:mm` | 每天几点跑 |
| `-AtStartup` | 增加开机触发（**默认以 SYSTEM 身份运行**，见下面「开机触发」） |
| `-AtLogon` | 增加登录时触发（适合经常关机的笔记本） |
| `-NoDaily` | 只要开机/登录触发，不要每天定时 |
| `-NetworkWait 秒` | 开机/登录触发后最多等多久让网络就绪，默认 120 |
| `-IntervalHours N` | 一天跑多次 |
| `-Args "--retry-failed"` | 透传给 `tieba_sign.py` 的参数 |
| `-Python <路径>` | 指定解释器；不填会从 PATH 自动解析成绝对路径 |
| `-RunAsUser` | 开机触发也改用当前用户身份（那就变回「登录后才跑」） |

行为说明：

- 改时间只要**再跑一次 install** 即可：它用 `Register-ScheduledTask -Force` 直接覆盖同名任务，不会堆出重复任务，也不会在失败时把旧任务弄丢。
- 任务配置了 `StartWhenAvailable`：到点时机器关着/睡着，开机后补跑一次。
- 关机充电相关限制都放开了（`AllowStartIfOnBatteries` / `DontStopIfGoingOnBatteries`），并加了失败重试 2 次、间隔 5 分钟。
- 同一次任务不会并发跑两份（`MultipleInstances IgnoreNew`）。
- 跑的结果追加到 `sign.log`，超过 1 MB 自动轮转成 `sign.log.1`、`sign.log.2`…（保留 5 份）。日志长这样：

  ```
  [2026-10-16 08:05:02] [wrapper] 等待网络就绪（最多 120 秒）...
  [2026-10-16 08:05:02] [wrapper] 网络探测: ping=True tcp443=True
  [2026-10-16 08:05:02] [wrapper] 网络就绪（ping tieba.baidu.com 通，等了约 0 秒），开始签到。
  [2026-10-16 08:05:02] [wrapper] 开始执行: python.exe -X utf8 ...\tieba_sign.py --log-path ...\sign.log
  2026-10-16T08:05:41 签到完成 共58 成功3 已签55 失败0 异常0 跳过0 耗时39.2s
  [2026-10-16 08:05:41] [wrapper] 结束，退出码 0，耗时 39.7s
  ```

  有失败时会多一段 `| 待处理: 某吧(签到失败/code=1102) ...`，直接 `Select-String 待处理 sign.log` 就能捞出问题吧。
- 用了非默认任务名/路径时，`status-task.ps1` 和 `uninstall-task.ps1` 会读 `task-config.json` 自动还原，不用重复传参。

### 开机触发：必须用 SYSTEM 身份，而且网络要等

「开机后自动签到」有两个坑，都是在真机上踩出来的：

**坑一：用当前用户身份注册，开机时任务根本不会启动。**
Windows 没有「网络就绪」这个触发器，只能用「开机触发 + 自己等」。但如果任务的身份是 `Interactive`（当前用户），Task Scheduler 只在这个用户**已登录**时才允许启动——开机那一刻会话还不存在，于是触发器到点了、任务被跳过，连「上次运行时间」都不更新。症状是：任务列得出来、触发器显示已启用、每天定时那次能跑，但**开机那次永远没动静**。

所以 `-AtStartup` 默认把身份设成 `SYSTEM`（`-LogonType ServiceAccount -RunLevel Highest`），这样开机后无需任何人登录即可运行。想改回当前用户就加 `-RunAsUser`。

**坑二：等网络的方式决定了会不会白等或误判。**
包装脚本 `run-sign.ps1` 的判断顺序是：

1. `ping tieba.baidu.com` —— 通 = DNS 正常且路由可达，最理想；
2. 不行就试 `TCP 连 443` —— **有些网络封 ICMP，ping 永远失败但网是好的**，只靠 ping 会白等满 120 秒；
3. 都不同则等到超时，仍然**照常执行**——签到脚本自己会重试，硬跳过等于白白漏掉一天。

探测结果变化时才记一行日志（`网络探测: ping=True tcp443=True`），不会刷屏。三处时间可以叠加控制：触发器的 `Delay`（默认 30 秒）+ `-NetworkWait`（默认 120 秒）+ 脚本自身的重试。

### 跑不起来时

| 现象 | 原因 / 处理 |
| --- | --- |
| `Register-ScheduledTask : 拒绝访问` | 需要**以管理员身份**注册。直接双击 `install-task.cmd` 最省事 |
| 开机那次没执行、定时那次正常 | 十有八九是身份问题：重新注册时用 `-AtStartup`（会自动切 SYSTEM），确认输出里写着 `运行身份 : SYSTEM` |
| 想看会注册成什么但先不写 | 加 `-WhatIf`（脚本是 `SupportsShouldProcess`）。注意**没有** `-Preview` 参数 |
| 任务状态是「已禁用」 | 任务计划程序里手动启用，或检查是否被安全软件改过 |
| 日志里写「PATH 上找不到 python」 | SYSTEM 的 PATH 是机器级 PATH，看不到「用户 PATH」里的 python。用 `install-task.ps1 -Python <完整路径>` 重新注册 |
| 日志里是乱码 | 子进程输出编码不一致所致。`run-sign.ps1` 已把三方对齐（python 加 `-X utf8`、`$OutputEncoding` 与 `[Console]::OutputEncoding` 都设 UTF-8）；如果你自己改过，注意这三处要一起改 |
| `.ps1` 脚本没输出、退出码却是 0 | PowerShell 5.1 按 ANSI（中文机器是 GBK）解析**没有 BOM** 的脚本，代码位置里的中文会变成乱码并破坏语法。本仓库的 `.ps1` 都带 UTF-8 BOM，改脚本时请用「UTF-8 带 BOM」保存 |
| 日志里只有 `找不到 BDUSS` | 计划任务用的工作目录不同，确认 cookie 在 `config.json` 里（而不是只设在某个终端的环境变量里） |
| 日志里是 `ReadTimeout`…统统连不上 | 先跑 `python tieba_sign.py --check`。代理会让直连超时 → 试 `--use-proxy`；反之网络必须过代理时也要加 `--use-proxy`；`--check` 会把系统代理和端口监听情况直接打出来 |
| `--check` 第①步 requests 不通、urllib 通 | 正常，`auto` 模式会自动选 urllib，不用管 |
| `--check` 第②步说 `is_login=0` | cookie 无效。浏览器重新登录 tieba.baidu.com，F12 → Application → Cookies，复制 `BDUSS` 的值 |

## 已验证 / 未验证

**已用真实账号端到端验证**：

- `python tieba_sign.py` 完整跑通（12 个关注的吧全部处理，排名正常显示），结果也写进了 `result.json`；
- 计划任务注册成功、`Start-ScheduledTask` 实跑多次，`sign.log` 里中文无乱码、退出码 0；
- **开机触发实跑成功**：任务身份为 `SYSTEM`，开机后自动等到网络再签到（这正是「坑一」那节的来源——用当前用户身份时开机那次从未执行）；
- 某些环境下 `requests` 后端打贴吧稳定 `ReadTimeout`，`urllib` 0.05s 就通，`--api auto` / `--transport auto` 都自动选中了 urllib。

**已在离线环境验证**：

- 语法、导入、`--help`、缺少 cookie 时的报错路径；
- `sign` 签名算法与 Node `crypto` 的 `encodeURIComponent` 版本**逐位一致**；
- **两个接口的签名行为（实测，用无效 cookie 也能定标）**：拿一个格式合法但无效的 BDUSS 去打接口，靠错误码区分「签名通过、卡在登录态」和「签名被拒」——

  | 签名 | Web `/sign/add` | 移动端 `/c/c/forum/sign` |
  | --- | --- | --- |
  | 算法正确 | `no=1990055` | `error_code=1`（用户未登录） |
  | 签名算错 / 随机 32 位 | `no=1990055`（**完全一样**） | `error_code=110001` |
  | 干脆不传 | `no=1990055`（**完全一样**） | `error_code=110001` |
  | 算法正确但 tbs 置 0 | — | `error_code=110001` |

  结论：Web 接口不校验签名；移动端接口校验，且 tbs 参与签名。这条不依赖真账号就能验证，也是 `--api auto` 自动退回策略的依据。
- `selftest.py`（离线、不起网络）覆盖：cookie 多种来源解析（含 `config.json` 的**字典与字符串两种写法**）、两种签名算法各自复算**且断言二者不相等**、移动端「已签到」是 `160002`、移动端 `110001` 触发退回 Web 且**只退一次**、移动端 `code=1` 仍正确识别为登录失效（不和 `110001` 混淆）、`user_sign_rank` 解析、移动端列表接口一次取回列表与已签集合、`--skip-signed` 真的少发请求、列表接口降级、签到过快退避重试、登录失效即中止且不再空跑、未知返回码、HTML 响应的可读报错、`tbs` 的 `is_login=0` 拦截、`auto` 自动选路与双后端全挂的报错、`main()` 到日志的完整落盘、`--check` 两条路径；
- `scripts/*.ps1` 全部通过 PowerShell 语法解析；`run-sign.ps1` 在 Windows PowerShell 5.1 下实跑过（含「python 报错也要把退出码和输出写进日志」这条路径）；
- 触发时间计算：`08:05` → 1 个点；`08:05 + 6h` → `08:05/14:05/20:05`；`23:30 + 1h` 与 `08:05 + 23h` 都只有 1 个点（不会跨天溢出或死循环）。

**未验证**：

- 长时间稳定性（连续多天跑、触发验证码/限流时的表现）；
- 百度改版后的兼容性——接口是非公开接口，改版了就需要同步调整抓取规则。

开发过程中真被坑到、已修掉的坑，列在这里免得再踩：

1. **`config.json` 里的 cookie 字典写法曾被忽略**：文档写了支持 `{"cookie": {"BDUSS": "..."}}`，代码却只认字符串，结果明明填了 cookie 却报「没有提供 cookie」。现在两种写法都支持（并会去掉值两端的引号和空白）。
2. **`.ps1` 没有 UTF-8 BOM**：PowerShell 5.1 按 ANSI（中文机器是 GBK）解析无 BOM 的脚本，代码位置里的中文会破坏语法——`status-task.ps1` 因此**静默退出 0 且毫无输出**。所有 `.ps1` 现在都带 BOM。
3. **日志乱码**：`run-sign.ps1` 已把 python 的 `-X utf8`、`$OutputEncoding`、`[Console]::OutputEncoding` 三方对齐成 UTF-8，`sign.log` 里中文正常。
4. **开机触发不执行**：见上面「坑一」。
5. **`install-task.ps1` 曾经「先卸载旧任务再注册新的」**：注册需要管理员权限，非管理员窗口里跑就变成「旧任务没了、新任务也没建上」。现在改成 `Register-ScheduledTask -Force` 直接覆盖，失败时旧任务保持不动。
6. **`install-task.cmd` 必须存成 GBK**：`cmd.exe` 按当前代码页读 `.bat`/`.cmd`，存成 UTF-8 中文全变乱码。

贴吧接口属于非公开接口，百度改版后可能需要调整。

## 注意事项

- BDUSS 等于账号凭证，**别提交、别外发**；泄露后到贴吧 App 退出登录即可让旧 BDUSS 失效。`.gitignore` 已挡住 `config.json` / `cookies.txt` / `sign.log` / `result.json` / `task-config.json`。
- 签到频繁可能触发验证码/限流（返回码 `2150040`、`2150041`），此时把 `--delay` 调大（比如 `5`）或当天作罢。
- 接口是贴吧自己的 Web 接口，百度改版后可能需要同步调整抓取规则；脚本把失败原因按返回码翻译成中文，方便定位。
- 仅供个人账号自动签到使用，请遵守贴吧的服务条款。

## License

MIT，见 [LICENSE](LICENSE)。
