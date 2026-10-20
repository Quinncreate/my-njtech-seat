# 我的占座

南京工业大学江浦校区的本地自习预约助手，当前版本 **v0.5.1**。在 Windows 上启动，用浏览器管理任务；Python 调度、Node.js 连接学校浏览器会话，日常预约和轮换不需要 AI API Key。

个人项目，非学校官方软件。当前适配逸夫、浦江两馆；其他学校的接口需要另外适配。

## 从这里开始

- [完整使用指南](docs/USAGE.md)：安装环境、首次登录、桌面快捷方式、双击选座、任务按钮和常见问题。
- [运行原理与方案](ARCHITECTURE.md)：学校接口、登录会话、后台调度与双账号接棒。
- [本地数据与隐私](PRIVACY.md)：数据保存位置、哪些内容不能提交或公开分享。

## 安装和启动

需要 **Python 3.11+**；真实预约另需 **Node.js 22+** 和 **Chrome 或 Edge**。无需额外 Python 包、npm 依赖或模型服务。

可选择 **Code → Download ZIP** 解压。安装 Git 的用户也可以在 PowerShell 执行：

```powershell
$seatFolder = Join-Path $env:USERPROFILE 'SeatWorkspace\project\my-njtech-seat'
git clone https://github.com/Quinncreate/my-njtech-seat.git "$seatFolder"
Set-Location "$seatFolder"
python .\server.py
```

随后打开 [本机任务页面](http://127.0.0.1:5003/dashboard/tasks)。建议保持 `SeatWorkspace\project\项目名` 的目录层级，程序会把缓存和测试文件放在同一工作区的 `tmp\项目名`。

日常双击 `启动我的占座.pyw` 即可隐藏终端启动，也可自行创建桌面快捷方式并选用 `static/njtech-emblem.ico`。仓库提供源码，不附带打包安装器或他人电脑的快捷方式。

## 首次使用

1. 首次启动默认是**模拟演示**，可以先体验虚拟座位和任务流程。
2. 结束模拟任务，在“学习概览”切换到“真实账号”。
3. 在 A、B 各自的学校浏览器窗口，分别登录**自己的两个不同账号**，自行处理验证码，并进入座位预约页面。仓库不提供账号，也不会继承其他人的登录。
4. 回到本机页面点击“验证登录”，等待两个账号就绪。运行期间保留学校浏览器窗口。
5. 新建任务，选择馆室，**双击白色空闲座位**，选中后变绿；红色不可选。
6. 默认“自习”、北京时间 07:00—22:00，可以修改。保存后还需点击“开始”；查询确认实际预约成功后才算持座。

登录过期后需要重新认证。首次登录与异常处理请阅读 [使用指南](docs/USAGE.md)。

## 主要功能

- 江浦两馆的实时空座与学校坐标图，搜索、缩放、手动及每 20 秒刷新。
- 暖色工作台，任务草稿、编辑、定时开始、暂停、恢复和复制重跑。
- 两个固定本机账号自动接棒，支持手动“切换至账号 A／B”。
- 点击“结束轮换”后核对并退掉本任务座位；暂停、计划结束和退出后台保留现有预约。
- 操作前保存意图，重启先核对学校实际预约；结果不明确时提示处理。
- v0.5.1 修复旧空闲暂停记录误拦截新任务；实际冲突会显示并定位具体任务。

## 使用边界

退座和另一账号预约是两次独立请求，存在网络故障或座位被他人先选走的可能，无法保证接棒成功。65 秒是当前程序设置的保护等待值，不代表学校明确规定所有情形均需等待 65 秒。

登录验证码自动识别尚未内置。已完成受控的真实预约、退座和双向接棒验证；完整长期自动调度与现场异常恢复仍待验证。电脑关机、休眠、断网或后台退出时不能执行轮换；关闭管理网页不会停止后台。

任务数据库、账号绑定、浏览器登录档案及调试记录均留在使用者电脑，不随仓库发布。反馈问题时请勿上传学号、密码、Cookie、令牌、任务库或原始网络抓包，见 [隐私说明](PRIVACY.md)。

## 源码与验证

| 文件 | 作用 |
| --- | --- |
| `server.py` | 仅监听本机 `127.0.0.1:5003` 的网页服务 |
| `seat_core.py` | 任务状态、接棒、冷却与 SQLite 持久化 |
| `live_provider.py` | 学校 GraphQL 适配和预约结果核对 |
| `browser_worker.js`、`browser_headers.js` | 独立浏览器档案、会话通信和请求头处理 |
| `seat_layout.py`、`static/` | 江浦馆室筛选、座位图和网页资源 |
| `tests/` | 不访问真实账号的状态机、适配器和座位筛选回归 |

在项目目录运行离线检查：

```powershell
$seatCache = Join-Path (Split-Path (Split-Path $PWD.Path -Parent) -Parent) ('tmp\' + (Split-Path $PWD.Path -Leaf))
$env:PYTHONPYCACHEPREFIX = Join-Path $seatCache 'python-cache'
python -m unittest discover -s tests -v
node --test tests/test_browser_headers.cjs
node .\browser_worker.js (Join-Path $seatCache 'browser-smoke') --smoke-test
```

当前有 72 项 Python 回归和 1 项 Node 本地 HTTP 请求头回归。浏览器通信检查只访问本机测试页面，不登录学校或操作真实预约。

设计参考见 [DESIGN_SOURCES.md](DESIGN_SOURCES.md)，校徽出处见 [static/EMBLEM_SOURCE.md](static/EMBLEM_SOURCE.md)。
