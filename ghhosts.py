#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ghhosts.py - Windows GitHub Hosts 自动更新工具

功能:
  1. 从 GitHub 官方 API (api.github.com/meta) 获取权威 IP 网段
  2. 向多个公共 DNS (1.1.1.1 / 8.8.8.8 / 9.9.9.9) 交叉解析
  3. 仅当多方结果一致时才写入,防止被污染数据污染 hosts
  4. 写入独立标记区块 (# ghdoctor-begin/end),不破坏用户自定义条目
  5. 写入前自动备份,支持一键回滚
  6. 写入后自动刷新 DNS 缓存并验证连通性

用法:
  ghhosts.exe                  # 更新 hosts (交互确认)
  ghhosts.exe --check          # 只检查,不写入
  ghhosts.exe --rollback       # 回滚到最近一次备份
  ghhosts.exe --status         # 查看当前 hosts 中的 GitHub 条目
  ghhosts.exe --install-task   # 注册计划任务 (每周自动更新)
  ghhosts.exe --uninstall-task # 移除计划任务
  ghhosts.exe --json out.json  # 输出 JSON 报告

需要管理员权限才能写入 hosts。
"""

import argparse
import ctypes
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------- 常量

HOSTS_PATH = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "drivers", "etc", "hosts")
BACKUP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                          "ghhosts", "backups")
BEGIN_MARK = "# ===== ghdoctor-begin (GitHub IPs, auto-generated) ====="
END_MARK = "# ===== ghdoctor-end ====="

# GitHub 官方 meta API 提供的网段 (仅作参考加权)
GITHUB_META_URL = "https://api.github.com/meta"

# 已知 GitHub 真实网段白名单(硬门槛)。
# 组成:
#   1) GitHub 官方 meta API 当前公布的段 (192.30.252.0/22 等)
#   2) GitHub 迁移后实际使用但 meta 尚未收录的段 (20.205.243.0/24 等)
#      —— 这些是实测 + 公开资料确认的 Azure 段
# 任何解析结果若不在本白名单内, 一律拒绝写入。
# 之所以需要白名单硬门槛: 多源 DNS 投票存在固有缺陷 ——
# 投毒 IP 是随机生成的, 偶尔会有两个源恰好返回同一个假 IP,
# 被误判为"一致"。实测 gist.github.com 就出现过这种情况。
KNOWN_GITHUB_CIDRS = [
    # --- 官方 meta 公布的段 ---
    "192.30.252.0/22",
    "185.199.108.0/22",
    "140.82.112.0/20",
    "143.55.64.0/20",
    "20.201.28.151/32",
    "20.205.243.166/32",
    # --- 新增: GitHub 当前实际使用的 Azure 段 (meta 未收录) ---
    "20.205.243.0/24",
    "20.27.177.0/24",
    "20.200.245.0/24",
    "4.208.26.0/24",
    "20.175.192.0/24",
    "20.26.156.0/24",
    "20.248.137.0/24",
    "20.207.73.0/24",
    "20.29.134.0/24",
    "4.237.22.0/24",
    "20.9.128.0/24",
    "20.253.160.0/24",
    "135.234.0.0/16",
    "20.233.83.0/24",
    # --- 通用兜底: Microsoft Azure 大段(GitHub 归属微软) ---
    # 注意: 过宽的段会降低拦截能力, 但能避免误杀真实 IP
    "20.0.0.0/8",
    "4.0.0.0/8",
    "13.64.0.0/11",
    "13.104.0.0/14",
    "40.64.0.0/10",
    "51.104.0.0/15",
    "52.0.0.0/8",
    "104.208.0.0/13",
    "135.0.0.0/9",
]

# 公共 DNS 服务器,用于交叉验证
PUBLIC_DNS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]

# 需要写入 hosts 的域名
TARGET_DOMAINS = [
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "gist.github.com",
    "github.io",
]

TASK_NAME = "GitHubHostsAutoUpdate"
DNS_TIMEOUT = 4.0


# ---------------------------------------------------------------- 工具函数

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def run_cmd(cmd, timeout=60, shell=False):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           shell=shell, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -2, "TIMEOUT"
    except FileNotFoundError:
        return -3, "NOT_FOUND"
    except Exception as e:
        return -1, str(e)


def log(msg, level="info"):
    tag = {"info": "  ", "ok": "[OK] ", "warn": "[!]  ", "err": "[X]  "}.get(level, "  ")
    print(tag + msg, flush=True)


# ---------------------------------------------------------------- DNS 查询

def udp_dns_query(name, server, timeout=DNS_TIMEOUT):
    """最小 DNS A 记录查询(不依赖外部工具)"""
    import random
    tid = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    q = b""
    for part in name.split("."):
        q += bytes([len(part)]) + part.encode()
    q += b"\x00" + struct.pack(">HH", 1, 1)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto(header + q, (server, 53))
        data, _ = s.recvfrom(4096)
        s.close()
    except Exception:
        return []
    if len(data) < 12:
        return []
    ancount = struct.unpack(">H", data[6:8])[0]
    if ancount == 0:
        return []
    idx = 12
    while idx < len(data) and data[idx] != 0:
        idx += data[idx] + 1
    idx += 5
    ips = []
    for _ in range(ancount):
        if idx >= len(data):
            break
        while idx < len(data):
            ln = data[idx]
            if ln == 0:
                idx += 1
                break
            if ln & 0xC0 == 0xC0:
                idx += 2
                break
            idx += ln + 1
        if idx + 10 > len(data):
            break
        rtype, _, _, rdlen = struct.unpack(">HHIH", data[idx:idx + 10])
        idx += 10
        if rtype == 1 and rdlen == 4:
            ips.append(".".join(str(b) for b in data[idx:idx + 4]))
        idx += rdlen
    return ips


def resolve_multi(name, servers=PUBLIC_DNS):
    """向多个 DNS 服务器解析,返回 {server: [ips]}"""
    result = {}
    for srv in servers:
        ips = udp_dns_query(name, srv)
        if ips:
            result[srv] = sorted(set(ips))
    return result


def majority_ip(name):
    """
    多源交叉解析,返回 (ip, detail, verdict)
    verdict: CONSISTENT / CONFLICT / NO_RESULT / POISONED

    污染检测增强:
      - 若各 DNS 源返回的 IP 完全互不相同(无任何交集), 且都在官方网段外
        -> 判定 POISONED (典型 DNS 投毒: 每个查询返回不同的随机 IP)
      - 只有 >=2 个独立 DNS 返回同一结果才认为可信
    """
    res = resolve_multi(name)
    if not res:
        return None, "所有 DNS 均无响应", "NO_RESULT"

    # 收集各源的完整 IP 列表(用于污染判定)
    all_by_src = {srv: set(ips) for srv, ips in res.items()}

    # 统计每个 IP 被多少个 DNS 源支持
    vote = {}
    for srv, ips in res.items():
        for ip in ips[:3]:  # 每个源最多取前3个,避免 CDN 轮询干扰
            vote.setdefault(ip, set()).add(srv)

    if not vote:
        return None, "无有效解析结果", "NO_RESULT"

    # 污染特征: 各源结果两两无交集, 且源数 >= 2
    if len(res) >= 2:
        every_ip_unique = all(len(s) == 1 for s in vote.values())
        any_shared = any(len(s) >= 2 for s in vote.values())
        if every_ip_unique and not any_shared:
            all_ips = sorted(vote.keys())
            return all_ips[0], ("各源返回互不相同的 IP: %s -> 典型投毒特征"
                                % ",".join(all_ips[:5])), "POISONED"

    best_ip, supporters = max(vote.items(), key=lambda kv: len(kv[1]))
    detail = "%s (由 %d 个源支持: %s)" % (best_ip, len(supporters),
                                          ",".join(sorted(supporters)))
    if len(supporters) >= 2:
        return best_ip, detail, "CONSISTENT"
    return best_ip, detail, "CONFLICT"


# ---------------------------------------------------------------- 官方网段校验

def fetch_github_meta(timeout=15):
    """从 GitHub 官方 API 获取 IP 网段。返回 (cidrs_list, source, error)"""
    from urllib.request import Request, urlopen
    import ssl
    # 优先直连(hosts 未生效时可能失败),失败则尝试环境代理
    for use_proxy in (False, True):
        try:
            ctx = ssl.create_default_context()
            req = Request(GITHUB_META_URL, headers={
                "User-Agent": "ghhosts/1.0",
                "Accept": "application/vnd.github+json",
            })
            opener = None
            if use_proxy:
                proxy = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
                         or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY"))
                if not proxy:
                    continue
                opener = __import__("urllib.request", fromlist=["build_opener"]).build_opener(
                    __import__("urllib.request", fromlist=["ProxyHandler"]).ProxyHandler(
                        {"https": proxy, "http": proxy}))
            if opener:
                resp = opener.open(req, timeout=timeout)
            else:
                resp = urlopen(req, timeout=timeout, context=ctx)
            with resp:
                data = json.loads(resp.read().decode("utf-8"))
            cidrs = list(data.get("git", [])) + list(data.get("web", [])) + \
                    list(data.get("api", [])) + list(data.get("pages", []))
            return sorted(set(cidrs)), ("proxy" if use_proxy else "direct"), None
        except Exception as e:
            err = str(e)[:120]
            continue
    return [], None, err if 'err' in dir() else "无法获取官方网段"


def ip_in_cidrs(ip, cidrs):
    """判断 IP 是否落在任一 CIDR 内"""
    try:
        ip_int = struct.unpack(">I", socket.inet_aton(ip))[0]
    except Exception:
        return False
    for cidr in cidrs:
        if "/" not in cidr:
            continue
        net, bits = cidr.split("/")
        try:
            bits = int(bits)
            net_int = struct.unpack(">I", socket.inet_aton(net))[0]
        except Exception:
            continue
        mask = (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF if bits else 0
        if (ip_int & mask) == (net_int & mask):
            return True
    return False


# ---------------------------------------------------------------- hosts 操作

def read_hosts():
    try:
        with open(HOSTS_PATH, "r", encoding="utf-8-sig", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except Exception as e:
        log("读取 hosts 失败: %s" % e, "err")
        return None


def extract_block(content):
    """提取标记区块内容,返回 (block_text, 是否存在)"""
    if BEGIN_MARK not in content or END_MARK not in content:
        return "", False
    start = content.index(BEGIN_MARK)
    end = content.index(END_MARK) + len(END_MARK)
    return content[start:end], True


def strip_block(content):
    """移除标记区块,返回 (剩余内容, 是否移除了)"""
    if BEGIN_MARK not in content or END_MARK not in content:
        return content, False
    start = content.index(BEGIN_MARK)
    end = content.index(END_MARK) + len(END_MARK)
    # 连同区块后的空行一起清理
    tail = content[end:]
    tail = tail.lstrip("\r\n")
    head = content[:start].rstrip("\r\n")
    return head + "\r\n\r\n" + tail if tail else head + "\r\n", True


def backup_hosts():
    """备份 hosts,返回备份路径"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(BACKUP_DIR, "hosts.%s.bak" % ts)
    try:
        shutil.copy2(HOSTS_PATH, dst)
        # 只保留最近 20 份
        baks = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith("hosts.")])
        for old in baks[:-20]:
            try:
                os.remove(os.path.join(BACKUP_DIR, old))
            except Exception:
                pass
        return dst
    except Exception as e:
        log("备份失败: %s" % e, "err")
        return None


def write_hosts(content):
    """写入 hosts(需要管理员权限)。先写临时文件再替换,避免写坏。"""
    tmp = HOSTS_PATH + ".ghhosts.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(content)
        # 保留原文件属性后替换
        shutil.move(tmp, HOSTS_PATH)
        return True, None
    except PermissionError:
        return False, "权限不足,请以管理员身份运行"
    except Exception as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        return False, str(e)


def flush_dns():
    rc, out = run_cmd(["ipconfig", "/flushdns"], timeout=20)
    return rc == 0


def latest_backup():
    if not os.path.isdir(BACKUP_DIR):
        return None
    baks = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith("hosts.")])
    return os.path.join(BACKUP_DIR, baks[-1]) if baks else None


# ---------------------------------------------------------------- 构建区块

def build_block(entries, meta_source):
    """entries: [(ip, domain, one_source_str)]"""
    lines = [BEGIN_MARK]
    lines.append("# 由 ghhosts 自动生成于 %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("# IP 来源: %s | 校验: 多源 DNS 交叉验证" % (meta_source or "public-dns"))
    lines.append("# 手动修改将被下次更新覆盖; 回滚用 ghhosts.exe --rollback")
    lines.append("")
    w = max(len(ip) for ip, _, _ in entries) if entries else 15
    for ip, domain, src in entries:
        lines.append("%-*s  %-32s # %s" % (w, ip, domain, src))
    lines.append("")
    lines.append(END_MARK)
    return "\r\n".join(lines)


# ---------------------------------------------------------------- 核心流程

def gather_ips(verbose=True):
    """
    收集可信 IP。返回 (entries, report)

    可信度模型(重要):
      - 硬门槛: 至少 2 个独立公共 DNS 返回一致结果 (CONSISTENT)
        —— 这是唯一必需的判据, 因为单源 DNS 可能被污染
      - 参考项: 是否落在 GitHub 官方 meta API 网段内
        —— 仅作加权提示, 不作拒绝依据
        原因: 官方 meta 网段列表存在滞后, github.com 已迁移到
        20.205.243.0/24 (Azure) 段, 但 meta API 仍只公布 4 个旧段。
        若用官方段做硬门槛, 会拒绝掉真实可用的 IP。
      - 额外防护: 拒绝解析到保留/内网地址 (0.x / 127.x / 10.x / 169.254.x)
    """
    if verbose:
        log("正在获取 GitHub 官方 IP 网段 (仅作参考加权) ...")
    cidrs, meta_src, meta_err = fetch_github_meta()
    if cidrs:
        if verbose:
            log("官方网段: 获取到 %d 条 (来源: %s)" % (len(cidrs), meta_src), "ok")
    else:
        if verbose:
            log("官方网段获取失败(%s),将仅依赖多源 DNS 交叉验证" % meta_err, "warn")

    RESERVED_PREFIX = ("0.", "127.", "10.", "169.254.", "255.", "224.",
                       "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
                       "172.2", "172.30.", "172.31.")

    # 合并白名单: 内置已知段 + 官方 meta 段
    whitelist = list(KNOWN_GITHUB_CIDRS)
    if cidrs:
        whitelist = sorted(set(whitelist) | set(cidrs))

    entries = []
    report = {"meta_source": meta_src, "meta_cidr_count": len(cidrs),
              "meta_error": meta_err, "whitelist_count": len(whitelist),
              "domains": {}}

    for domain in TARGET_DOMAINS:
        ip, detail, verdict = majority_ip(domain)
        item = {"domain": domain, "ip": ip, "detail": detail,
                "verdict": verdict, "in_official_range": None, "accepted": False}
        if ip is None:
            if verbose:
                log("%-32s 解析失败,跳过" % domain, "warn")
            report["domains"][domain] = item
            continue

        # 防护 1: 保留/内网地址一律拒绝(污染特征)
        if ip.startswith(RESERVED_PREFIX):
            if verbose:
                log("%-32s %s -> 解析到保留地址,拒绝写入 (疑似污染)" % (domain, ip), "err")
            report["domains"][domain] = item
            continue

        # 防护 2 (硬门槛): IP 必须在已知 GitHub 网段白名单内
        # 这是拦截"随机投毒 IP 恰好被两源撞上"的关键防线
        in_wl = ip_in_cidrs(ip, whitelist)
        item["in_official_range"] = in_wl
        if not in_wl:
            if verbose:
                log("%-32s %s -> 不在任何已知 GitHub 网段内,拒绝写入" % (domain, ip), "err")
                log("%-32s    判定为 DNS 投毒 (即使多源一致也不可信)" % "", "warn")
            item["poisoned"] = True
            report["domains"][domain] = item
            report.setdefault("poisoned_domains", []).append(domain)
            continue

        # 防护 3: 多源一致
        if verdict == "POISONED":
            if verbose:
                log("%-32s 检测到 DNS 投毒! %s" % (domain, detail), "err")
            item["poisoned"] = True
            report["domains"][domain] = item
            report.setdefault("poisoned_domains", []).append(domain)
            continue
        if verdict != "CONSISTENT":
            if verbose:
                log("%-32s %s -> %s,拒绝写入" % (domain, ip, verdict), "warn")
            report["domains"][domain] = item
            continue

        note = "%s; 白名单校验通过" % verdict
        item["accepted"] = True
        if verbose:
            log("%-32s %-18s %s" % (domain, ip, detail), "ok")
        entries.append((ip, domain, note))
        report["domains"][domain] = item

    return entries, report


def cmd_update(args):
    print("=" * 66)
    print("  GitHub Hosts 自动更新工具  |  %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 66)

    content = read_hosts()
    if content is None:
        return 1

    entries, report = gather_ips(verbose=True)

    print("\n" + "-" * 66)
    if not entries:
        log("没有获得任何可信 IP,不修改 hosts。", "err")
        log("可能原因: 网络完全不可达 / DNS 被污染 / 官方网段获取失败", "warn")
        log("建议: 先解决基础连通性(可改用 SSH:22 或 SSH over 443)", "warn")
        return 2

    poisoned = report.get("poisoned_domains", [])
    if poisoned:
        print()
        log("以下域名检测到 DNS 投毒,已跳过: %s" % ", ".join(poisoned), "warn")
        log("这些域名无法用 hosts 解决(hosts 只能修正 IP,不能绕过投毒)", "warn")
        log("建议: 启用加密 DNS (DoH/DoT),参见治理方案文档方案 A", "warn")

    old_block, had_block = extract_block(content)
    new_block = build_block(entries, report["meta_source"])

    print("准备写入 %d 条记录:" % len(entries))
    for ip, domain, _ in entries:
        print("   %-18s %s" % (ip, domain))

    if had_block:
        old_lines = [l for l in old_block.splitlines() if l and not l.startswith("#")]
        new_lines = [l for l in new_block.splitlines() if l and not l.startswith("#")]
        if old_lines == new_lines:
            log("hosts 中的 GitHub 记录已是最新,无需更新。", "ok")
            if not args.json:
                return 0

    if args.check:
        log("--check 模式: 未写入 hosts。", "info")
        if args.json:
            write_json(args.json, entries, report, "check-only")
        return 0

    if not is_admin():
        log("需要管理员权限才能写入 hosts。", "err")
        log("请右键以管理员身份运行,或使用 --check 仅预览。", "warn")
        return 3

    # 备份
    bak = backup_hosts()
    if bak:
        log("已备份原 hosts: %s" % bak, "ok")
    else:
        log("备份失败,中止写入以保护系统。", "err")
        return 4

    # 组装新内容
    base, removed = strip_block(content)
    new_content = base.rstrip("\r\n") + "\r\n\r\n" + new_block + "\r\n"

    ok, err = write_hosts(new_content)
    if not ok:
        log("写入失败: %s" % err, "err")
        log("可从备份恢复: %s" % bak if bak else "", "warn")
        return 5
    log("hosts 写入成功。", "ok")

    if flush_dns():
        log("DNS 缓存已刷新。", "ok")
    else:
        log("DNS 缓存刷新失败,请手动执行 ipconfig /flushdns", "warn")

    # 验证
    print("\n" + "-" * 66)
    log("连通性验证中 ...")
    ok_count, fail_count = verify_connectivity()
    log("验证结果: %d 个域名可解析, %d 个失败" % (ok_count, fail_count),
        "ok" if fail_count == 0 else "warn")

    if args.json:
        write_json(args.json, entries, report, "updated")
        log("报告已写出: %s" % args.json)

    return 0


def verify_connectivity():
    ok_count = fail_count = 0
    for domain in TARGET_DOMAINS[:5]:
        try:
            ip = socket.gethostbyname(domain)
            print("   [OK] %-32s -> %s" % (domain, ip))
            ok_count += 1
        except Exception:
            print("   [X]  %-32s 解析失败" % domain)
            fail_count += 1
    return ok_count, fail_count


def write_json(path, entries, report, status):
    out = {
        "time": datetime.now().isoformat(),
        "status": status,
        "hosts_path": HOSTS_PATH,
        "entries": [{"ip": ip, "domain": d, "verify": s} for ip, d, s in entries],
        "report": report,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("写 JSON 失败: %s" % e, "err")


def cmd_check(args):
    args.check = True
    return cmd_update(args)


def cmd_rollback(args):
    print("=" * 66)
    print("  回滚 hosts")
    print("=" * 66)
    bak = latest_backup()
    if not bak:
        log("未找到备份文件 (%s)" % BACKUP_DIR, "err")
        return 1
    log("使用备份: %s" % bak)
    if not args.yes:
        try:
            ans = input("确认恢复? (y/N) ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans not in ("y", "yes"):
            log("已取消。", "warn")
            return 0
    if not is_admin():
        log("需要管理员权限。", "err")
        return 3
    try:
        shutil.copy2(bak, HOSTS_PATH)
        log("已恢复 hosts。", "ok")
        if flush_dns():
            log("DNS 缓存已刷新。", "ok")
        return 0
    except Exception as e:
        log("恢复失败: %s" % e, "err")
        return 5


def cmd_status(args):
    content = read_hosts()
    if content is None:
        return 1
    print("hosts 路径: %s" % HOSTS_PATH)
    print("文件大小: %d 字节" % len(content))
    block, has = extract_block(content)
    if not has:
        log("未找到 ghdoctor 标记区块 (hosts 未被本工具修改过)", "warn")
        return 0
    log("找到标记区块:", "ok")
    print()
    for line in block.splitlines():
        print("   " + line)
    print()
    baks = sorted([f for f in os.listdir(BACKUP_DIR)
                   if f.startswith("hosts.")]) if os.path.isdir(BACKUP_DIR) else []
    print("备份数量: %d" % len(baks))
    if baks:
        print("最近备份: %s" % os.path.join(BACKUP_DIR, baks[-1]))
    return 0


# ---------------------------------------------------------------- 计划任务

def cmd_install_task(args):
    print("=" * 66)
    print("  注册计划任务 (每周自动更新 hosts)")
    print("=" * 66)
    if not is_admin():
        log("需要管理员权限。", "err")
        return 3
    exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    if getattr(sys, "frozen", False):
        cmd = '"%s" --silent' % exe
    else:
        cmd = '"%s" "%s" --silent' % (sys.executable, os.path.abspath(__file__))
    # 每周一 09:00
    xml_cmd = (
        'schtasks /Create /TN "%s" /TR "%s" /SC WEEKLY /D MON /ST 09:00 '
        '/RL HIGHEST /F' % (TASK_NAME, cmd.replace('"', r'\"'))
    )
    rc, out = run_cmd(xml_cmd, timeout=30, shell=True)
    if rc == 0:
        log("计划任务已注册: %s (每周一 09:00)" % TASK_NAME, "ok")
        log("查看: schtasks /Query /TN %s" % TASK_NAME)
        log("移除: ghhosts.exe --uninstall-task")
        return 0
    log("注册失败: %s" % out.strip()[:200], "err")
    return 5


def cmd_uninstall_task(args):
    rc, out = run_cmd('schtasks /Delete /TN "%s" /F' % TASK_NAME,
                      timeout=30, shell=True)
    if rc == 0:
        log("计划任务已移除。", "ok")
        return 0
    log("移除失败: %s" % out.strip()[:200], "err")
    return 5


# ---------------------------------------------------------------- 入口

def interactive_menu():
    """双击运行时的友好交互菜单"""
    while True:
        print()
        print("=" * 66)
        print("  GitHub Hosts 工具  |  管理员: %s" % ("是" if is_admin() else "否"))
        print("=" * 66)
        print("  1. 检查并更新 hosts (推荐)")
        print("  2. 仅检查, 不修改        (预览将要写入的 IP)")
        print("  3. 查看当前 GitHub 条目  (--status)")
        print("  4. 回滚到上次备份        (--rollback)")
        print("  5. 注册每周自动更新      (--install-task)")
        print("  6. 移除自动更新任务      (--uninstall-task)")
        print("  0. 退出")
        print("=" * 66)
        try:
            ch = input("请选择 [0-6]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if ch == "0":
            return 0
        elif ch == "1":
            class A:
                check = False
                json = None
                yes = True
            r = cmd_update(A())
            print()
        elif ch == "2":
            class A:
                check = True
                json = None
                yes = True
            r = cmd_update(A())
            print()
        elif ch == "3":
            cmd_status(args=None)
        elif ch == "4":
            class A:
                yes = False
            cmd_rollback(A())
        elif ch == "5":
            cmd_install_task(args=None)
        elif ch == "6":
            cmd_uninstall_task(args=None)
        else:
            print("  无效选择, 请重新输入。")


def main():
    ap = argparse.ArgumentParser(
        description="Windows GitHub Hosts 自动更新工具",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--check", action="store_true", help="只检查并打印将写入的内容,不修改 hosts")
    ap.add_argument("--rollback", action="store_true", help="回滚到最近一次备份")
    ap.add_argument("--status", action="store_true", help="查看当前 hosts 中的 GitHub 条目")
    ap.add_argument("--install-task", action="store_true", help="注册每周自动更新的计划任务")
    ap.add_argument("--uninstall-task", action="store_true", help="移除计划任务")
    ap.add_argument("--json", metavar="FILE", help="输出 JSON 报告")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认")
    ap.add_argument("--silent", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if os.name != "nt":
        log("本工具为 Windows 专用 (hosts 路径依赖 Windows 系统目录)。", "err")
        return 3

    # 计划任务调用的静默模式: 直接更新, 无交互
    if args.silent:
        class A:
            check = False
            json = args.json
            yes = True
        return cmd_update(A())

    if args.rollback:
        return cmd_rollback(args)
    if args.status:
        return cmd_status(args)
    if args.install_task:
        return cmd_install_task(args)
    if args.uninstall_task:
        return cmd_uninstall_task(args)
    if args.check:
        return cmd_check(args)
    if args.yes:
        return cmd_update(args)

    # 无参数 -> 交互菜单
    return interactive_menu()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
