# ghdoctor — GitHub 网络访问分层诊断与治理工具

单文件、纯标准库、零依赖、跨平台（Windows / macOS / Linux）。

针对 GitHub 无法解析或访问（DNS 污染 / 连接重置 / 证书劫持）的问题，按
**DNS → 连通 → TLS → 应用层** 四层逐层诊断，判定故障归属，并给出可用通道与治理建议。

## 快速开始

```bash
# 完整诊断（推荐）
python ghdoctor.py

# 快速诊断（跳过 TLS 与权威 DNS 对比，约 10 秒）
python ghdoctor.py --quick

# 抓间歇性阻断（重要！很多"时好时坏"必须靠采样才能看出）
python ghdoctor.py --samples 5

# 只测三条可用通道
python ghdoctor.py --channels

# 生成 hosts 修复片段（只生成，不写入）
python ghdoctor.py --fix-hosts

# 重新解析并对比 GitHub IP（IP 漂移后自查）
python ghdoctor.py --update-ips

# 输出机器可读结果
python ghdoctor.py --json result.json
```

## 参数

| 参数 | 说明 |
|---|---|
| `--quick` | 跳过 TLS 证书链与权威 DNS 对比（快，但少两层结论） |
| `--samples N` | TCP 连通性采样 N 次，识别间歇性阻断（**排查"时通时不通"必用**） |
| `--channels` | 只检测 SSH:22 / SSH:443 / HTTPS 三条通道，给出推荐通道 |
| `--fix-hosts` | 从权威 DNS 获取真实 IP 并生成 hosts 片段（不写入文件） |
| `--update-ips` | 重新解析并对比系统与权威 DNS，判断 IP 是否失效 |
| `--json FILE` | 把完整诊断结果写成 JSON |

## 退出码

- `0` 有可用通道
- `2` 全部不可用（需按治理方案处理）
- `3` 参数错误 / 中断

## 状态标记含义

| 标记 | 含义 | 对应判定 |
|---|---|---|
| `[OK]` | 正常 | — |
| `[SUSPECT-POISON]` | 系统解析结果与权威 DNS 无交集，或解析到保留地址 | **DNS 污染** |
| `[DNS-FAIL]` | 解析失败 | 本地 DNS 故障或域名被劫持 |
| `[TIMEOUT]` | 连接超时 | 端口被阻断 / 丢包 |
| `[RESET]` | 连接被重置 | 主动干扰（RST 注入） |
| `[REFUSED]` | 连接被拒绝 | 端口关闭 |
| `[FLAKY]` | 多次采样部分成功 | **间歇性阻断**（最易误判） |
| `[CERT-ALERT]` | 证书校验失败 / 域名不匹配 | **疑似中间人劫持** |
| `[TLS-FAIL]` | TLS 握手失败 | 协议层干扰 |
| `[MISSING]` | 本机缺少工具（git/ssh/dig） | 环境问题 |

## 局限性

- `[CERT-ALERT]` 里的 `unable to get local issuer certificate` 在部分 Python / OpenSSL
  环境下只是**本机缺 CA bundle**，不一定是劫持。必须用 `openssl s_client` 交叉验证
  （见 `GitHub网络访问排查与治理方案.md` 第 3 层），不要仅凭本工具结论下"被劫持"的断言。
- 权威 DNS 对比依赖能访问 1.1.1.1 / 8.8.8.8；若这些也被阻断会退化为 `UNKNOWN`。
- 无 `dig` 且非 Windows 时使用内置最小 DNS 查询，只支持 A 记录与单层压缩指针。
