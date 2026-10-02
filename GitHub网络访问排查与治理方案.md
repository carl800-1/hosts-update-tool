# GitHub 网络访问排查与治理方案

> 适用症状：GitHub 打不开 / clone 卡住 / 时通时不通 / 报 SSL 证书错误 / DNS 解析异常
> 覆盖平台：Windows 10/11、macOS、Linux
> 覆盖场景：git CLI、网页访问、SSH 拉取
>
> 配套工具：`ghdoctor.py`（本目录，单文件零依赖），一条命令跑完四层诊断。

---

## 〇、问题定位速查表

先看症状，直接跳到结论，再用后面对应层的命令验证。

| 症状表现 | 最可能层 | 判定结论 | 直接跳到 |
|---|---|---|---|
| `nslookup` 返回 0.0.0.0 / 127.0.0.1 / 陌生 IP | DNS | **DNS 污染** | §1 |
| 解析结果与 1.1.1.1 查询结果完全不同 | DNS | **DNS 污染** | §1 |
| 浏览器显示"连接超时"，`ping` 通但 443 不通 | 连通 | **443 端口阻断** | §2 |
| 连接秒断，报 `Connection reset by peer` | 连通 | **RST 注入干扰** | §2 |
| 时通时不通，重试可能成功 | 连通 | **间歇性阻断/QoS 限速** | §2 |
| 浏览器报"证书不受信任"/`ERR_CERT_AUTHORITY_INVALID` | TLS | **中间人劫持** | §3 |
| 证书 CN 不是目标域名、签发者陌生 | TLS | **证书劫持** | §3 |
| `git clone` 报 `SSL certificate problem` | TLS/应用 | 证书问题或 CA 缺失 | §3 + §4 |
| clone 报 `Failed to connect to github.com:443 after 21s` | 连通 | 443 阻断，改 SSH | §2 → §4 |
| `git push` 反复提示输密码 | 应用 | 认证方式问题（非网络） | §4 |
| `ssh -T git@github.com` 成功但 clone 慢 | 应用 | 网络可用，可能限速 | §4 |

**实测参考（本机 2026-10-02 状态）**：DNS 正常、`github.com:443` 间歇阻断（采样 2/4 成功）、
SSH:22 与 SSH:443 均通、TLS 证书正常 → 结论是"**DNS 与证书都好，只有 443 被针对性干扰**"。

---

## 一、第 1 层：DNS 解析

### 1.1 诊断命令

**Windows**
```bat
nslookup github.com
nslookup github.com 1.1.1.1
nslookup github.com 8.8.8.8
ipconfig /displaydns | findstr /i github
```

**macOS / Linux**
```bash
dig github.com A +short
dig @1.1.1.1 github.com A +short
dig @8.8.8.8 github.com A +short
# 无 dig 时：
nslookup github.com
host github.com
```

**跨平台（Python，无需安装任何东西）**
```bash
python ghdoctor.py --quick      # 看第 1 层输出
python ghdoctor.py --update-ips # 只看 IP 对比
```

### 1.2 判定结论

| 观察结果 | 判定 |
|---|---|
| 系统解析结果 ∈ {1.1.1.1, 8.8.8.8} 结果集合 | ✅ **DNS 正常** |
| 系统解析结果与公共 DNS **无交集** | ❌ **DNS 污染** |
| 返回 `0.0.0.0`、`127.0.0.1`、`10.x`、`169.254.x` | ❌ **DNS 劫持/污染**（黑洞或本地重定向） |
| 解析超时 / NXDOMAIN | ❌ 本地 DNS 故障或被投毒 |

> **注意**：GitHub 有多个真实 IP 段（`20.205.243.x`、`140.82.121.x`、`185.199.108-111.x`）。
> 系统解析到 `20.205.243.166` 与权威 DNS 一致，就是正常的 —— **不要看到"IP 变了"就以为被污染**。
> GitHub 的 `raw.githubusercontent.com` 用 185.199.108-111 四个 IP 做轮询，属于正常 CDN 行为。

### 1.3 治理（若判定污染）

见 §5 方案 A（加密 DNS）或方案 B（hosts 绑定）。

---

## 二、第 2 层：网络连通

### 2.1 诊断命令

**Windows**
```bat
ping github.com
tracert -d -h 15 github.com
:: 测试 443（需 PowerShell）
powershell -Command "Test-NetConnection github.com -Port 443"
powershell -Command "Test-NetConnection github.com -Port 22"
powershell -Command "Test-NetConnection ssh.github.com -Port 443"
:: 连续测试 5 次看是否间歇
powershell -Command "1..5 | %% { (Test-NetConnection github.com -Port 443 -WarningAction SilentlyContinue).TcpTestSucceeded }"
```

**macOS / Linux**
```bash
ping -c 4 github.com
traceroute -n -m 15 github.com
nc -vz -w 5 github.com 443
nc -vz -w 5 github.com 22
nc -vz -w 5 ssh.github.com 443
# 连续 5 次（Linux）
for i in $(seq 5); do timeout 3 bash -c "echo >/dev/tcp/github.com/443" \
  && echo "OK" || echo "FAIL"; done
# macOS 无 /dev/tcp，用 nc 循环：
for i in $(seq 5); do nc -z -w 3 github.com 443 && echo OK || echo FAIL; done
```

**启动 curl 直接看结果（跨平台）**
```bash
curl -sS -o /dev/null -w "code=%{http_code} ip=%{remote_ip} t=%{time_total}\n" \
  --max-time 12 https://github.com
# 绕过所有代理做真实直连测试：
curl -sS -o /dev/null -w "%{http_code}\n" --max-time 12 --noproxy '*' https://github.com
```

**用工具一次性测 8 个目标 + 采样**
```bash
python ghdoctor.py --samples 5
```

### 2.2 判定结论

| 观察结果 | 判定 |
|---|---|
| `ping` 通 + 443 通 | ✅ 连通正常 |
| `ping` 通 + 443 超时 | ❌ **443 端口被阻断**（典型 GFW QoS） |
| `ping` 不通 + 443 也不通 | ❌ 路由中断或整体封锁 |
| 连接立即被断开 / `Connection reset` | ❌ **RST 注入**（主动干扰） |
| 多次测试有的成功有的失败 | ❌ **间歇性阻断**——最容易误判，**必须采样** |
| 只有 `github.com:443` 不通，其他域名 443 都通 | ❌ **针对该域名的定向干扰** |
| 只有 22 不通，443 通 | 22 被封 → 用 SSH over 443（方案 D） |

> **实战要点**：`git ls-remote` 单次失败不能证明被墙，可能是偶发丢包。
> 必须 `--samples 5` 采样，或手动循环 5 次。本机实测 `github.com:443` 是 2/4 成功，
> 属于间歇阻断 —— 这类问题靠"重试"有时能蒙过去，但极不稳定。

### 2.3 治理（若判定阻断）

- 443 通、22 不通 → 方案 D（SSH over 443）
- 443 不通、22 通 → 直接改用 SSH（方案 C）
- Both 都不通 → 方案 E（代理）或方案 F（镜像）

---

## 三、第 3 层：TLS / 证书

### 3.1 诊断命令

**Windows**
```bat
:: Win10+ 自带 curl
curl -vI https://github.com 2>&1 | findstr /i "subject issuer SSL certificate"
:: 需要 OpenSSL（Git for Windows 自带 openssl）
"C:\Program Files\Git\usr\bin\openssl.exe" s_client -connect github.com:443 -servername github.com < NUL | findstr /i "subject issuer Verify"
```

**macOS / Linux**
```bash
echo | openssl s_client -connect github.com:443 -servername github.com 2>/dev/null \
  | grep -E "subject=|issuer=|Verify return code|Protocol|Cipher"
# 只看证书链
echo | openssl s_client -showcerts -connect github.com:443 -servername github.com 2>/dev/null
# 用系统 CA 严格校验（关键！）
curl -vI https://github.com 2>&1 | grep -iE "SSL|certificate|issuer"
```

**用工具自动判定**
```bash
python ghdoctor.py   # 看第 3 层输出
```

### 3.2 判定结论

| 观察结果 | 判定 |
|---|---|
| `subject=CN=github.com`，签发者为 DigiCert / Sectigo 等主流 CA | ✅ **证书正常** |
| `Verify return code: 0 (ok)` | ✅ 系统信任链完整 |
| `Verify return code: 20 (unable to get local issuer certificate)` + 证书 CN 正确 | ⚠️ **多为本机缺 CA bundle，不一定是劫持** |
| 证书 CN 与目标域名**不符**（如 CN=SomeFirewall） | ❌ **中间人劫持** |
| 签发者是陌生 CA / 自签名证书 | ❌ **证书劫持** |
| `curl` 报 `SSL certificate problem: self signed certificate` | ❌ 劫持或杀软 HTTPS 扫描 |

> **重要辨析**：`Verify return code: 20` 极易误判。它只表示 **openssl 命令行找不到本机 CA 库**，
> 不代表 GitHub 证书有问题。本机实测就是 20，但证书是 `CN=github.com` + Sectigo 签发 + TLS1.3，
> 完全正常。**必须交叉验证 CN 与签发者**，两者都对就是环境问题，不是劫持。

### 3.3 治理（若真劫持）

1. **不要关闭证书校验**（`git config --global http.sslVerify false` 是危险操作，会让中间人可读写你的代码）
2. 检查是否有杀毒/安全软件的"HTTPS 扫描"功能，关闭它
3. 检查系统根证书库是否被植入了陌生根证书：
   - Windows：`certmgr.msc` → 受信任的根证书颁发机构
   - macOS：`security find-certificate -a -p /Library/Keychains/System.keychain | openssl x509 -noout -subject`
   - Linux：`ls /usr/local/share/ca-certificates/`
4. 用 §5 方案 E（可信代理）绕开劫持点

---

## 四、第 4 层：应用层（git CLI / 网页 / SSH）

### 4.1 git CLI

**诊断命令**
```bash
# 查看是否配了代理（很多问题源于失效代理）
git config --global --list | grep -i proxy
git config --global --get-regexp 'url\.'

# 绕过代理直连测试（关键手段）
GIT_TERMINAL_PROMPT=0 git -c http.proxy= -c https.proxy= \
  ls-remote https://github.com/carl800-1/spms.git HEAD

# SSH 方式测试
GIT_TERMINAL_PROMPT=0 git ls-remote git@github.com:carl800-1/spms.git HEAD

# SSH over 443 测试
GIT_TERMINAL_PROMPT=0 git -c core.sshCommand="ssh -p 443" \
  ls-remote git@ssh.github.com:carl800-1/spms.git HEAD

# 打开 git 调试日志看卡在哪一步
GIT_CURL_VERBOSE=1 GIT_TRACE=1 git clone <url> 2>&1 | head -50
```

**判定**

| 结果 | 判定 |
|---|---|
| `ls-remote` 返回 40 位 hash | ✅ 该通道可用 |
| `Failed to connect ... after 21s` | ❌ 443 阻断 |
| `SSL certificate problem` | ❌ 证书问题（§3） |
| `Could not resolve host` | ❌ DNS 问题（§1） |
| 走代理时报 `Failed to connect to github.com:443 over proxy` | ❌ **代理失效**，需更新或清除代理 |

> **本机关键坑**：环境里存在 `https_proxy=http://127.0.0.1:56128`，
> 但该代理对 github 分流失效（15s 超时）；记忆里的 `127.0.0.1:28432` 已完全不存在（端口拒绝）。
> **凡是走代理失败，先确认代理端口还活着。**

**清除失效代理（可回退）**
```bash
# 清除（git 层面）
git config --global --unset http.proxy
git config --global --unset https.proxy
# 恢复（如需）
git config --global http.proxy http://127.0.0.1:7890
```

### 4.2 网页访问

**诊断**
```bash
# 看返回码与耗时
curl -sS -o /dev/null -w "code=%{http_code} t=%{time_total}\n" https://github.com
# 看是否走代理成功
curl -sS -o /dev/null -w "code=%{http_code}\n" -x http://127.0.0.1:7890 https://github.com
# 浏览器同款请求（带 UA）
curl -sS -I -A "Mozilla/5.0" https://github.com | head -5
```

**判定**：`code=200` 表示通；`code=000` 表示连接未建立（阻断/DNS/证书均可能导致）。

### 4.3 SSH 拉取

**诊断**
```bash
ssh -T git@github.com                    # 标准 22 端口
ssh -T -p 443 git@ssh.github.com         # 443 端口
ssh -vT git@github.com 2>&1 | head -30   # 详细握手过程
```

**判定**

| 输出 | 判定 |
|---|---|
| `Hi <user>! You've successfully authenticated` | ✅ **该端口可用** |
| `Connection timed out` | ❌ 端口被阻断 |
| `Permission denied (publickey)` | ⚠️ 网络通，密钥未配置 |
| `Host key verification failed` | ⚠️ known_hosts 冲突，需清理 |

**清理 known_hosts（可回退，先备份）**
```bash
cp ~/.ssh/known_hosts ~/.ssh/known_hosts.bak
ssh-keygen -R github.com
ssh-keygen -R "[ssh.github.com]:443"
```

### 4.4 全局开关：临时禁代理验证

```bash
# Windows (Git Bash / PowerShell)
set https_proxy= & set http_proxy=          :: cmd
$env:https_proxy=$null; $env:http_proxy=$null  # PowerShell
# macOS / Linux
unset https_proxy http_proxy HTTPS_PROXY HTTP_PROXY
```

---

## 五、治理方案对比表

按风险从低到高排列，**优先选上面的**。

| # | 方案 | 生效范围 | 维护成本 | 风险 | 适用场景 |
|---|---|---|---|---|---|
| **C** | **改用 SSH（22 端口）** | git CLI 全部操作 | 低（一次性配好） | 低（加密，官方支持） | 22 通、443 不通 |
| **D** | **SSH over 443** | git CLI 全部操作 | 低（一行 url 改写） | 低（复用官方 443） | 22 被封但 443 通 |
| **A** | **加密 DNS（DoH/DoT）** | 全系统 DNS | 中（配置一次） | 低 | DNS 污染 |
| **B** | **hosts 绑定真实 IP** | 全系统（该域名） | **高（IP 漂移需维护）** | 中（IP 变了反而更糟） | 污染严重且 DNS 无法加密 |
| **E** | **配置代理** | 可精细化到 git/浏览器 | 中（代理需保持可用） | 中（代理可信度影响安全） | 全网阻断、需访问多站点 |
| **F** | **镜像加速（ghproxy）** | 只读拉取/下载 | 低 | **中高（第三方可见流量）** | 仅拉取依赖、临时救急 |
| ~~G~~ | ~~关闭 SSL 校验~~ | — | — | **极高（可被篡改代码）** | **不推荐，仅调试** |

### 方案 C：改用 SSH（22 端口）

```bash
# 生成密钥（若已有跳过）
ssh-keygen -t ed25519 -C "your@email.com"
cat ~/.ssh/id_ed25519.pub   # 复制到 GitHub → Settings → SSH keys

# 验证
ssh -T git@github.com
# 期望：Hi <user>! You've successfully authenticated...

# 已有仓库切换远程
git remote set-url origin git@github.com:carl800-1/spms.git
# 回退：
git remote set-url origin https://github.com/carl800-1/spms.git
```
**生效范围**：该仓库的 git 操作。**成本**：极低。**风险**：低。
**前提**：22 端口可通。

### 方案 D：SSH over 443（推荐用于 443 干扰环境）

```bash
# 方式一：仅当前仓库
git remote set-url origin ssh://git@ssh.github.com:443/carl800-1/spms.git

# 方式二：全局改写（所有 github SSH 地址自动走 443）
git config --global url."git@ssh.github.com:443/".insteadOf "git@github.com:"
# 回退：
git config --global --unset url."git@ssh.github.com:443/".insteadOf

# 验证
ssh -T -p 443 git@ssh.github.com
```

也可写入 `~/.ssh/config`（对所有 ssh 生效）：
```
Host github.com
  HostName ssh.github.com
  Port 443
  User git
```
**生效范围**：git CLI + 任何 ssh。**成本**：低。**风险**：低（复用 GitHub 官方 443 服务）。
**前提**：`ssh.github.com:443` 可通。

### 方案 A：加密 DNS

**Windows 11 (DoH)**
```powershell
# 查看网卡名
Get-NetAdapter | Select-Object Name,InterfaceIndex
# 设置 DoH 服务器
Set-DnsClientDohServerAddress -ServerAddress 1.1.1.1 -AllowFallbackToUdp $false -AutoUpgrade $true
Set-DnsClientDohServerAddress -ServerAddress 8.8.8.8 -AllowFallbackToUdp $false -AutoUpgrade $true
# 验证
Get-DnsClientDohServerAddress
# 回退
Remove-DnsClientDohServerAddress -ServerAddress 1.1.1.1
```
Win10 需手动在"网络适配器 → IPv4 属性 → 首选 DNS"填 `1.1.1.1`，并安装 Cloudflare WARP 或使用支持 DoH 的浏览器。

**macOS**：安装 `.mobileconfig` 描述文件（Cloudflare/Quad9 官网提供），或
```bash
# 临时改用公共 DNS
sudo networksetup -setdnsservers Wi-Fi 1.1.1.1 8.8.8.8
# 回退
sudo networksetup -setdnsservers Wi-Fi Empty
```

**Linux (systemd-resolved + DoT)**
```bash
# /etc/systemd/resolved.conf
sudo tee /etc/systemd/resolved.conf <<'EOF'
[Resolve]
DNS=1.1.1.1#cloudflare-dns.com 8.8.8.8#dns.google
DNSOverTLS=yes
EOF
sudo systemctl restart systemd-resolved
resolvectl status | grep -A3 "DNS Servers"
# 回退：注释掉上面两行后 restart
```
**生效范围**：全系统。**成本**：中。**风险**：低（但仍可能被 SNI 层干扰）。

### 方案 B：hosts 绑定（IP 会漂移，谨慎）

```bash
# 用工具生成最新片段
python ghdoctor.py --fix-hosts

# 备份 → 写入 → 刷新
# Windows（管理员）
copy "C:\Windows\System32\drivers\etc\hosts" "C:\Windows\System32\drivers\etc\hosts.bak"
# 追加（用记事本或 PowerShell）
Add-Content -Path "C:\Windows\System32\drivers\etc\hosts" -Value "20.205.243.166 github.com"
ipconfig /flushdns
# 回退：还原 .bak 后 flushdns

# macOS / Linux
sudo cp /etc/hosts /etc/hosts.bak
echo "20.205.243.166 github.com" | sudo tee -a /etc/hosts
sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder   # macOS
sudo resolvectl flush-caches                                     # Linux
# 回退：sudo cp /etc/hosts.bak /etc/hosts
```
**生效范围**：全系统。**成本**：**高（GitHub IP 数月漂移一次）**。
**风险**：中 —— **IP 失效后会造成比不做更严重的故障**（连都不通）。
**必读**：只在 DNS 确实被污染、且无法使用加密 DNS 时使用。

### 方案 E：代理

```bash
# git 走 HTTP 代理
git config --global http.proxy  http://127.0.0.1:7890
git config --global https.proxy http://127.0.0.1:7890
# 只对 github 走代理，其他直连
git config --global http.https://github.com/.proxy http://127.0.0.1:7890
# 回退
git config --global --unset http.proxy
git config --global --unset https.proxy

# SSH 走代理（SOCKS5，需 ~/.ssh/config）
# Host github.com
#   ProxyCommand nc -X 5 -x 127.0.0.1:7891 %h %p
```
**生效范围**：git / 浏览器 / 终端（取决于代理软件的分流规则）。
**成本**：中（代理端口变化时要改配置）。**风险**：中（**代理方可见未加密流量**，务必用可信代理）。
**关键提醒**：本机历史上代理分流失效过，**配置代理后必须用 `curl -x` 单独验证代理本身是否工作**。

### 方案 F：镜像加速（仅只读、临时）

```bash
# 临时用镜像 clone（不要改全局配置）
git clone https://ghproxy.net/https://github.com/carl800-1/spms.git
# 或改 hosts 指向镜像（不推荐长期用）

# 下载 release / raw 文件走镜像
curl -L https://ghproxy.net/https://raw.githubusercontent.com/user/repo/main/file
```
**生效范围**：单次命令。**成本**：低。**风险**：**中高** —— 镜像方可见你拉取的源码，
且镜像可能失效或投毒。**绝不要**把 push 流量走镜像。

---

## 六、恢复验证标准

治理后必须**逐层复验**，不能只看"clone 成功一次"。

### 6.1 验收清单

```bash
# ① DNS 层
python ghdoctor.py --update-ips
#    标准：所有域名 verdict=OK

# ② 连通层（采样，排除间歇性）
python ghdoctor.py --samples 5
#    标准：目标通道 5/5 成功，无 FLAKY

# ③ TLS 层
python ghdoctor.py
#    标准：第 3 层全部 [OK]

# ④ 应用层：连续 3 次 ls-remote 全部成功
for i in 1 2 3; do
  GIT_TERMINAL_PROMPT=0 git ls-remote git@github.com:carl800-1/spms.git HEAD \
    && echo "第 $i 次 OK" || echo "第 $i 次 FAIL"
done

# ⑤ 端到端：真实 clone 到临时目录
git clone git@github.com:carl800-1/spms.git /tmp/spms-verify && \
  du -sh /tmp/spms-verify && rm -rf /tmp/spms-verify
```

### 6.2 通过标准

| 检查项 | 通过标准 |
|---|---|
| DNS | 系统解析与权威 DNS 一致（或 hosts 生效） |
| 连通 | 采样 5 次全成功，无 TIMEOUT/RESET/FLAKY |
| TLS | 证书 CN 正确、签发者为主流 CA、无告警 |
| git 读 | 连续 3 次 `ls-remote` 成功 |
| git 写 | `git push` 一次小改动成功且无重试 |
| 稳定期 | **连续 3 天**日常使用无中断才算真正稳定 |

---

## 七、后续维护建议

### 7.1 IP 失效的识别与更新

**识别信号**：原本正常的 clone 突然超时；`ping github.com` 解析到陌生 IP；
浏览器报连接超时但昨天还好。

```bash
# 一键检测 IP 是否需更新
python ghdoctor.py --update-ips
# 若提示"解析不一致"，重新生成 hosts 片段：
python ghdoctor.py --fix-hosts
```

**更新流程（hosts 方案）**
1. `python ghdoctor.py --fix-hosts` 取最新 IP
2. 备份 hosts → 替换旧 IP 行 → 刷新 DNS
3. 跑 §6.1 验收清单

### 7.2 权威 IP 来源（手动查询）

- GitHub 官方元数据（**最权威**）：
  ```bash
  curl -s https://api.github.com/meta | grep -A5 '"git"'
  ```
- 公共 DNS 直查：`dig @1.1.1.1 github.com A +short`
- 第三方实时库：`https://raw.hellogithub.com/hosts`（社区维护，用前自行核对）

### 7.3 维护节奏建议

| 周期 | 动作 |
|---|---|
| 每周 | 跑一次 `ghdoctor.py --quick`，确认通道健康 |
| 每月 | `--samples 5 --update-ips`，检查间歇阻断与 IP 漂移 |
| 变更网络后 | 立即重跑完整诊断（换 WiFi、换运营商、开 VPN 都会改变结论） |
| IP 失效时 | `--fix-hosts` 更新 + §6.1 验收 |

### 7.4 长期建议：优先 SSH，慎用 hosts

- **推荐长期态**：方案 C（SSH:22）+ 方案 D（SSH over 443）双通道，写进 `~/.ssh/config`，
  两个端口互为备份，任一被封都能用。
- **不推荐长期用 hosts**：维护成本高、IP 漂移会造成比不做更糟的故障。
  只在 DNS 污染且无法加密 DNS 时作为过渡。
- **定期清理失效代理**：`git config --global --list | grep proxy`，
  发现指向已关闭端口的代理立即 `--unset`。

---

## 附：一键诊断脚本

```bash
python ghdoctor.py --samples 5 --json gh-report.json
```

输出 JSON 含四层全部原始数据与 verdict，可存档用于前后对比。

---

*文档生成时间：2026-10-02 | 配套工具：ghdoctor.py*
