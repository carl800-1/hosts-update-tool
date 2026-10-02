# GitHub Hosts 自动更新工具

Windows 专用。自动获取 GitHub 真实 IP 并写入 hosts，解决 DNS 解析异常导致的 GitHub 访问问题。

**核心设计：宁可不写，也不写错。** 所有 IP 必须通过三重校验才会写入。

---

## 两种版本

| 版本 | 文件 | 适用 |
|---|---|---|
| **图形界面版** | `dist\GitHubHosts.exe` | 双击即用，推荐给日常使用 |
| 命令行版 | `dist\GitHubHostsUpdater.exe` | 脚本调用、计划任务 |

两个 exe 功能完全一致，共用同一套核心逻辑。

---

## 快速开始

### 方式一：双击启动器（推荐）

双击 **`启动Hosts工具.bat`** —— 自动请求管理员权限并打开图形界面。

### 方式二：直接运行 GUI

```bat
dist\GitHubHosts.exe
```

### 方式三：命令行

```bat
dist\GitHubHostsUpdater.exe            :: 交互菜单
dist\GitHubHostsUpdater.exe --check    :: 只预览，不改 hosts
dist\GitHubHostsUpdater.exe --yes      :: 直接更新（需管理员）
```

> GUI 版也支持命令行参数透传：
> `dist\GitHubHosts.exe --check` 会走控制台模式（但有窗口模式下看不到输出，
> 建议命令行操作直接用 `GitHubHostsUpdater.exe`）。

---

## 图形界面说明

```
┌──────────────────────────────────────────────────────┐
│  GitHub Hosts 自动更新          ● 管理员权限         │
├──────────────────────────────────────────────────────┤
│  hosts: C:\Windows\System32\drivers\etc\hosts         │
│  已写入标记区块: 否  |  备份: 3 份                     │
├──────────────────────────────────────────────────────┤
│  域名              解析 IP          校验结果   状态   │
│  github.com        20.205.243.166  白名单通过  可用   │
│  gist.github.com   203.98.7.65     网段外      投毒   │
├──────────────────────────────────────────────────────┤
│  [检查] [更新 hosts] [刷新DNS] [回滚] [计划任务]      │
├──────────────────────────────────────────────────────┤
│  [10:23:41] 开始检查 GitHub 解析状态 ...              │
└──────────────────────────────────────────────────────┘
```

**颜色含义**：绿 = 可用 / 红 = 投毒 / 黄 = 警告

**按钮功能**：

| 按钮 | 作用 |
|---|---|
| 检查 | 解析所有域名并校验（不改 hosts） |
| 更新 hosts | 弹确认框后写入（非管理员时自动提权） |
| 刷新 DNS | 执行 `ipconfig /flushdns` |
| 回滚 | 恢复到最近一次备份 |
| 计划任务 | 注册/移除每周自动更新 |
| 导出报告 | 导出 JSON 或 TXT 检查报告 |
| 查看已写入 | 显示 hosts 中本工具写入的区块 |

---

## 三重校验机制

这是本工具和网上"一键 hosts"脚本最大的区别。

| # | 校验 | 作用 |
|---|---|---|
| 1 | **保留地址拦截** | 拒绝 `0.x` / `127.x` / `10.x` / `169.254.x` —— 污染特征 |
| 2 | **已知网段白名单（硬门槛）** | IP 必须在 GitHub 真实网段内，否则一律拒绝 |
| 3 | **多源 DNS 交叉验证** | 至少 2 个独立公共 DNS（1.1.1.1/8.8.8.8/9.9.9.9）返回同一结果 |

**为什么白名单是硬门槛？** 实测发现多源投票有固有缺陷：DNS 投毒返回的是**随机 IP**，
偶尔会有两个源恰好撞到同一个假 IP，被误判为"一致"。

> 实测案例：`gist.github.com` 两次运行分别得到
> `59.24.3.173 / 78.16.49.15 / 8.7.198.45`（三个源全不同）和
> `203.98.7.65`（两源撞上同一个假 IP）。
> 前者被多源投票拦截，后者**只能靠白名单拦截**。

---

## 命令参考

| 命令 | 说明 |
|---|---|
| `--check` | 只检查并预览将写入的内容，**不修改 hosts** |
| `--yes` | 跳过确认直接更新（需管理员） |
| `--status` | 查看当前 hosts 中的 GitHub 条目与备份列表 |
| `--rollback` | 回滚到最近一次备份 |
| `--install-task` | 注册计划任务（每周一 09:00 自动更新） |
| `--uninstall-task` | 移除计划任务 |
| `--json FILE` | 输出 JSON 报告 |
| `--silent` | 静默更新（供计划任务调用） |

---

## 安全性设计

### 独立标记区块

只操作自己管理的区块，**绝不碰你的自定义条目**：

```
# 你自己的条目 —— 不受影响
127.0.0.1  myapp.local

# ===== ghdoctor-begin (GitHub IPs, auto-generated) =====
20.205.243.166    github.com    # CONSISTENT; 白名单校验通过
...
# ===== ghdoctor-end =====
```

### 自动备份

每次写入前自动备份到 `%LOCALAPPDATA%\ghhosts\backups\`，自动保留最近 20 份。

### 原子写入

先写临时文件再替换，避免写一半断电导致 hosts 损坏。

### 权限门禁

非管理员运行时**拒绝写入**；GUI 版会自动弹出 UAC 提权请求。

---

## 回滚

```bat
dist\GitHubHostsUpdater.exe --rollback
```

或手动恢复：
```bat
copy "%LOCALAPPDATA%\ghhosts\backups\hosts.20261002-081230.bak" ^
     "C:\Windows\System32\drivers\etc\hosts"
ipconfig /flushdns
```

---

## 自动更新（计划任务）

```bat
:: 以管理员身份运行
dist\GitHubHostsUpdater.exe --install-task
```

注册成功后每周一 09:00 自动更新。查看/移除：
```bat
schtasks /Query  /TN GitHubHostsAutoUpdate
dist\GitHubHostsUpdater.exe --uninstall-task
```

---

## 从源码重新打包

```bat
build-gui.bat
```

一键打包两个版本（GUI + 控制台）。产物在 `dist\`。

**依赖**：需要带 tkinter 的完整 Python（GUI 版必须）。
本机路径：`C:\Users\Frank\AppData\Local\Programs\Python\Python313`

手动打包：
```bat
pip install pyinstaller

:: GUI 版（无控制台窗口）
pyinstaller --onefile --windowed --name GitHubHosts ^
    --add-data "ghhosts.py;." ghhosts_gui.py

:: 控制台版
pyinstaller --onefile --console --name GitHubHostsUpdater ghhosts.py
```

---

## 已知限制（重要）

### 1. hosts 无法解决所有 DNS 问题

hosts 只能**修正域名到 IP 的映射**。如果遇到以下情况，hosts 无能为力：

- **DNS 投毒**：本工具会检测出并跳过，但无法绕过。需改用**加密 DNS（DoH/DoT）**
- **TLS 证书劫持**：必须在系统根证书层面处理
- **443 端口阻断**：这是网络层问题，与 DNS 无关

### 2. 你的环境实际情况（2026-10-02 实测）

| 项目 | 状态 |
|---|---|
| `github.com` DNS | ✅ 正常（20.205.243.166） |
| `github.com:443` | ⚠️ **间歇性阻断**（采样 2/4 成功） |
| `gist.github.com` DNS | ❌ **检测到投毒**（每次返回不同随机 IP） |
| SSH:22 / SSH:443 | ✅ 均可用 |

**结论：你的主要问题不是 DNS，而是 443 端口干扰。**
hosts 更新对你帮助有限 —— 真正的解法是走 SSH 通道：

```bat
git config --global url."git@ssh.github.com:443/".insteadOf "git@github.com:"
```

详细的完整排查方案见同目录 `GitHub网络访问排查与治理方案.md`。

### 3. IP 会漂移

GitHub IP 数月变更一次。建议开启计划任务自动更新，或定期点「检查」核对。

### 4. 白名单需维护

内置白名单（`ghhosts.py` 中 `KNOWN_GITHUB_CIDRS`）包含 GitHub 官方 meta 段
加上实测确认的新段。GitHub 若启用全新网段，可能被误拦 ——
此时运行「检查」会看到"不在任何已知 GitHub 网段内"，
需把新网段加进白名单后重新打包。

---

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 读取 hosts 失败 |
| 2 | 无可信 IP，未修改 |
| 3 | 权限不足 / 非 Windows |
| 4 | 备份失败（主动中止） |
| 5 | 写入或计划任务操作失败 |

---

*最后更新：2026-10-02*
