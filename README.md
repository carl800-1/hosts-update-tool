# Hosts Update Tool

Windows 下自动更新 hosts，解决 DNS 解析异常导致的 GitHub 访问问题。

**核心设计：宁可不写，也不写错。** 所有 IP 必须通过三重校验才会写入 hosts。

---

## 为什么需要它

GitHub 在国内经常出现 DNS 解析异常、连接被重置、证书劫持等问题。
常见做法是往 hosts 里写死 GitHub 的 IP，但网上的"一键脚本"有个通病：
**无脑写入** —— 遇到被投毒的 IP 照写不误，反而让问题更糟。

本项目做了三件事：

1. **多源校验** —— 只有多个独立 DNS 返回一致结果才采信
2. **网段白名单** —— IP 必须落在已知的 GitHub 真实网段内，否则拒绝
3. **安全写入** —— 独立标记区块 + 自动备份 + 一键回滚，绝不动你的自定义条目

---

## 功能

- 自动获取 GitHub 真实 IP（官方 meta API + 多源 DNS 交叉验证）
- 图形界面（tkinter），表格颜色标识每个域名的校验状态
- 命令行模式，支持计划任务自动更新
- **DNS 投毒检测** —— 识别并跳过被投毒的域名，而不是写入假 IP
- 独立标记区块写入，不破坏 hosts 中的其他条目
- 自动备份（保留最近 20 份）+ 一键回滚

---

## 快速开始

### 下载

从 [Releases](../../releases) 下载最新版：

| 文件 | 说明 |
|---|---|
| `GitHubHosts.exe` | 图形界面版，双击即用 |
| `GitHubHostsUpdater.exe` | 命令行版，供脚本 / 计划任务调用 |

### 使用

```
1. 双击 GitHubHosts.exe
2. 点击「检查」，查看各域名解析状态
3. 点击「更新 hosts」，确认为写入
```

非管理员运行时，写入前会自动弹出 UAC 提权请求。

### 命令行

```bat
GitHubHostsUpdater.exe --check           :: 只预览，不修改
GitHubHostsUpdater.exe --yes             :: 直接更新（需管理员）
GitHubHostsUpdater.exe --status          :: 查看已写入条目
GitHubHostsUpdater.exe --rollback        :: 回滚到上次备份
GitHubHostsUpdater.exe --install-task    :: 注册每周自动更新
GitHubHostsUpdater.exe --uninstall-task  :: 移除计划任务
GitHubHostsUpdater.exe --json out.json   :: 输出 JSON 报告
```

---

## 三重校验机制

| # | 校验 | 作用 |
|---|---|---|
| 1 | **保留地址拦截** | 拒绝 `0.x` / `127.x` / `10.x` / `169.254.x` —— 污染特征 |
| 2 | **已知网段白名单（硬门槛）** | IP 必须在 GitHub 真实网段内，否则一律拒绝 |
| 3 | **多源 DNS 交叉验证** | 至少 2 个独立公共 DNS（1.1.1.1 / 8.8.8.8 / 9.9.9.9）返回同一结果 |

### 为什么白名单是硬门槛

多源投票有个固有缺陷：DNS 投毒返回的是**随机 IP**，偶尔会有两个源
恰好撞到同一个假 IP，被误判为"一致"。

实测案例：某域名两次运行分别得到

- `59.24.3.173` / `78.16.49.15` / `8.7.198.45` —— 三个源全不同
- `203.98.7.65` —— 两个源撞上同一个假 IP

前者被多源投票拦截，**后者只能靠白名单拦截**。

---

## 安全机制

### 独立标记区块

只操作自己管理的区块，绝不动你的自定义条目：

```
# 你自己的条目 —— 不受影响
127.0.0.1  myapp.local

# ===== ghdoctor-begin (GitHub IPs, auto-generated) =====
20.205.243.166    github.com    # CONSISTENT; 白名单校验通过
...
# ===== ghdoctor-end =====
```

### 自动备份 + 原子写入

每次写入前备份到 `%LOCALAPPDATA%\ghhosts\backups\`（保留 20 份）。
写入采用"先写临时文件再替换"的方式，避免中途失败损坏 hosts。

### 权限门禁

非管理员运行时拒绝写入，不会静默失败。

---

## 已知限制

hosts 只能修正**域名到 IP 的映射**，以下情况无法解决：

- **DNS 投毒** —— 本工具会检测出来并跳过，但无法绕过。需改用加密 DNS（DoH/DoT）
- **TLS 证书劫持** —— 需在系统根证书层面处理
- **端口阻断** —— 属于网络层问题，与 DNS 无关。GitHub 443 被阻断时建议改用 SSH：

  ```bat
  git config --global url."git@ssh.github.com:443/".insteadOf "git@github.com:"
  ```

### 其他

- GitHub IP 数月变更一次，建议开启计划任务（`--install-task`）自动更新
- 白名单在 `ghhosts.py` 的 `KNOWN_GITHUB_CIDRS` 中维护。
  GitHub 若启用全新网段可能被误拦，此时检查结果会显示"不在任何已知 GitHub 网段内"，
  需把新网段加进白名单后重新打包

---

## 从源码构建

需要带 tkinter 的完整 Python（3.8+）。

```bat
pip install pyinstaller
build-gui.bat
```

产物在 `dist\`（GUI 版 + 控制台版）。

手动打包：

```bat
pyinstaller --onefile --windowed --name GitHubHosts ^
    --add-data "ghhosts.py;." ghhosts_gui.py

pyinstaller --onefile --console --name GitHubHostsUpdater ghhosts.py
```

> **注意**：`--add-data` 的相对路径是相对 spec 目录，
> 建议用绝对路径，否则会报 `Unable to find ...`。

### 直接运行源码

```bat
python ghhosts_gui.py     :: 图形界面
python ghhosts.py         :: 命令行
```

---

## 项目结构

```
ghhosts.py              核心逻辑（校验 + hosts 读写 + 命令行）
ghhosts_gui.py          tkinter 图形界面
ghdoctor.py             网络分层诊断工具（DNS / TCP / TLS / 应用层）
build-gui.bat           一键打包双版本
build.bat               仅打包控制台版
启动Hosts工具.bat        双击启动器（自动提权）
更新Hosts.bat            控制台版启动器
Hosts工具使用说明.md     详细使用文档
GitHub网络访问排查与治理方案.md   完整网络排查方法论
```

### 附带的诊断工具 ghdoctor

用于定位问题根源，判断到底是 DNS、连通、TLS 还是应用层的问题：

```bat
python ghdoctor.py --samples 5     :: 四层诊断（DNS/连通/TLS/应用层）
python ghdoctor.py --channels      :: 检测 SSH22 / SSH443 / HTTPS 可用性
python ghdoctor.py --fix-hosts     :: 生成 hosts 修复片段
python ghdoctor.py --update-ips    :: 检查 IP 是否漂移
```

其中 `--samples` 是关键 —— **间歇性阻断必须多次采样才能发现**，单次结果会误判。

---

## License

MIT
