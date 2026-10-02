#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ghdoctor.py - GitHub 网络访问分层诊断与治理工具 (单文件 / 纯标准库 / 跨平台)

分层: DNS -> TCP/连通 -> TLS/证书 -> 应用层(git / HTTP / SSH)
平台: Windows / macOS / Linux
用途: 定位 DNS 污染、连接重置、证书劫持；给出可用通道与治理建议。

用法:
    python ghdoctor.py                 # 完整诊断
    python ghdoctor.py --quick         # 快速诊断(跳过耗时项)
    python ghdoctor.py --json out.json # 输出机器可读结果
    python ghdoctor.py --channels      # 只测三条可用通道(SSH22/SSH443/HTTPS)
    python ghdoctor.py --fix-hosts     # 生成 hosts 修复片段(不写入)
    python ghdoctor.py --update-ips    # 重新解析并对比推荐 IP

退出码: 0=有可用通道, 2=全部不可用, 3=参数错误
需要网络。不需要任何第三方库。需要本机有 ssh / openssl(可选,缺失自动降级)。
"""

import argparse
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time

# ---------------------------------------------------------------- 配置常量

# GitHub 相关域名的权威 DNS(GitHub 官方/Azure 常用) —— 用于对比判断污染
AUTHORITATIVE_DNS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]

# 关键目标: 名称 -> (端口, 协议, 说明)
TARGETS = [
    ("github.com", 443, "tcp", "GitHub Web / HTTPS git 端点"),
    ("github.com", 22, "tcp", "GitHub SSH 标准端口"),
    ("ssh.github.com", 443, "tcp", "GitHub SSH over 443 (穿越封锁首选)"),
    ("api.github.com", 443, "tcp", "GitHub REST API"),
    ("codeload.github.com", 443, "tcp", "仓库归档下载 (clone/zip)"),
    ("raw.githubusercontent.com", 443, "tcp", "raw 文件 / 脚本下载"),
    ("objects.githubusercontent.com", 443, "tcp", "release 附件 / LFS 对象"),
    ("github.io", 443, "tcp", "GitHub Pages"),
]

# 证书校验期望: 域名 -> 期望出现在 CN/SAN 中的字符串
CERT_EXPECT = {
    "github.com": "github.com",
    "api.github.com": "github.com",
    "codeload.github.com": "github.com",
    "ssh.github.com": "github.com",
    "raw.githubusercontent.com": "githubusercontent.com",
}

# 已知会伪造/劫持证书的机构(仅用于告警提示,不参与判定逻辑的硬依赖)
KNOWN_LEGIT_ISSUERS = ("DigiCert", "Sectigo", "GlobalSign", "Let's Encrypt", "Amazon", "Google Trust")

DEFAULT_TIMEOUT = 6.0


# ---------------------------------------------------------------- 平台适配

def plat() -> str:
    """返回 win / mac / linux / other"""
    s = platform.system().lower()
    if s.startswith("win"):
        return "win"
    if s == "darwin":
        return "mac"
    if s == "linux":
        return "linux"
    return "other"


def c(code: str, text: str) -> str:
    """ANSI 着色; 不支持时原样返回"""
    if os.name == "nt" and not os.environ.get("WT_SESSION") and not os.environ.get("TERM"):
        return text  # 老式 cmd 可能不支持,保守退化
    if not sys.stdout.isatty():
        return text
    return "\033[%sm%s\033[0m" % (code, text)


def ok(s):   return c("32", s)
def bad(s):  return c("31", s)
def warn(s): return c("33", s)
def dim(s):  return c("90", s)
def bold(s): return c("1", s)


# ---------------------------------------------------------------- 基础探测

def run_cmd(cmd, timeout=DEFAULT_TIMEOUT):
    """执行命令,返回 (rc, stdout+stderr)。失败返回 (-1, err)"""
    try:
        p = subprocess.run(
            cmd, shell=isinstance(cmd, str),
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -2, "TIMEOUT"
    except FileNotFoundError:
        return -3, "NOT_FOUND"
    except Exception as e:  # noqa
        return -1, str(e)


def resolve_system(name: str):
    """使用操作系统解析器解析域名"""
    try:
        infos = socket.getaddrinfo(name, None, socket.AF_INET)
        ips = sorted({i[4][0] for i in infos})
        return ips
    except Exception as e:  # noqa
        return None


def resolve_authoritative(name: str, server: str, timeout=4.0):
    """
    向指定 DNS 服务器查询 A 记录。
    优先 dig/nslookup; 都不可用时回退 UDP 裸查询(自实现最小 DNS 报文)。
    """
    if shutil.which("dig"):
        cmd = ["dig", "+short", "+time=3", "+tries=1", "@" + server, name, "A"]
        rc, out = run_cmd(cmd, timeout=timeout + 2)
        if rc == 0:
            ips = [l.strip() for l in out.splitlines()
                   if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", l.strip())]
            if ips:
                return sorted(set(ips))
    if plat() == "win" and shutil.which("nslookup"):
        rc, out = run_cmd(["nslookup", "-type=A", name, server], timeout=timeout + 2)
        if rc == 0:
            # 取非服务器段的 Address 行
            ips = re.findall(r"Address:\s*(\d{1,3}(?:\.\d{1,3}){3})", out)
            ips = [i for i in ips if not i.startswith("192.168.")
                   and i != server]
            if ips:
                return sorted(set(ips))
    return udp_dns_query(name, server, timeout)


def udp_dns_query(name: str, server: str, timeout=3.0):
    """最小 DNS A 记录查询实现(不依赖外部工具)"""
    import struct, random
    tid = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    q = b""
    for part in name.split("."):
        q += bytes([len(part)]) + part.encode()
    q += b"\x00" + struct.pack(">HH", 1, 1)  # A, IN
    pkt = header + q
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto(pkt, (server, 53))
        data, _ = s.recvfrom(2048)
        s.close()
    except Exception:  # noqa
        return None
    if len(data) < 12:
        return None
    ancount = struct.unpack(">H", data[6:8])[0]
    if ancount == 0:
        return None
    # 跳过 header + question
    idx = 12
    while idx < len(data) and data[idx] != 0:
        idx += data[idx] + 1
    idx += 5  # 0x00 + qtype(2) + qclass(2)
    ips = []
    for _ in range(ancount):
        if idx >= len(data):
            break
        # name (可能被压缩)
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
    return sorted(set(ips)) if ips else None


def tcp_connect_once(host: str, port: int, timeout=DEFAULT_TIMEOUT):
    """
    单次 TCP 连接。返回 (status, detail)
    status: OK / REFUSED / TIMEOUT / DNS_FAIL / RESET / ERR
    """
    try:
        t0 = time.time()
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        if not infos:
            return "DNS_FAIL", "no A record"
        ip = infos[0][4][0]
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        dt = time.time() - t0
        s.close()
        return "OK", "ip=%s time=%.2fs" % (ip, dt)
    except socket.timeout:
        return "TIMEOUT", ">%.0fs" % timeout
    except ConnectionRefusedError:
        return "REFUSED", "port closed / refused"
    except ConnectionResetError:
        return "RESET", "connection reset by peer"
    except socket.gaierror as e:
        return "DNS_FAIL", str(e)
    except OSError as e:
        msg = str(e)
        if "reset" in msg.lower():
            return "RESET", msg
        if "refus" in msg.lower():
            return "REFUSED", msg
        return "ERR", msg


def tcp_connect(host: str, port: int, timeout=DEFAULT_TIMEOUT, samples=1):
    """
    多次采样 TCP 连接, 识别间歇性阻断。
    返回 (status, detail); status 可能是 FLAKY(部分成功)。
    """
    if samples <= 1:
        return tcp_connect_once(host, port, timeout)
    got = []
    for i in range(samples):
        st, detail = tcp_connect_once(host, port, timeout)
        got.append((st, detail))
    oks = [g for g in got if g[0] == "OK"]
    if len(oks) == samples:
        return "OK", oks[0][1]
    if oks:
        return "FLAKY", "%d/%d 成功 (间歇阻断) 例:%s" % (len(oks), samples, oks[0][1])
    # 全部失败, 取最高频状态
    from collections import Counter
    st = Counter(g[0] for g in got).most_common(1)[0][0]
    return st, got[0][1]


def tls_inspect(host: str, port=443, timeout=8.0):
    """
    返回 dict: {status, subject, issuer, sans, version, error, self_signed, mismatch}
    status: OK / CERT_MISMATCH / SELF_SIGNED / HANDSHAKE_FAIL / ERR
    """
    out = {"status": "ERR", "subject": "", "issuer": "",
           "sans": [], "version": "", "error": "", "mismatch": False}
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert()
                out["version"] = ss.version()
                out["subject"] = dict(x[0] for x in cert.get("subject", ())).get("commonName", "")
                out["issuer"] = dict(x[0] for x in cert.get("issuer", ())).get("commonName", "")
                out["sans"] = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
                out["status"] = "OK"
    except ssl.SSLCertVerificationError as e:
        out["status"] = "CERT_MISMATCH"
        out["error"] = str(e)
        # 尝试不校验再取一次, 看是谁签的(用于识别劫持方)
        try:
            ctx2 = ssl._create_unverified_context()  # noqa
            with socket.create_connection((host, port), timeout=timeout) as sock:
                with ctx2.wrap_socket(sock, server_hostname=host) as ss:
                    cert = ss.getpeercert()
                    out["subject"] = dict(x[0] for x in cert.get("subject", ())).get("commonName", "")
                    out["issuer"] = dict(x[0] for x in cert.get("issuer", ())).get("commonName", "")
        except Exception:  # noqa
            pass
        exp = CERT_EXPECT.get(host, host.split(".")[-2] + "." + host.split(".")[-1])
        if exp not in (out["subject"] or ""):
            out["mismatch"] = True
    except ssl.SSLError as e:
        out["status"] = "HANDSHAKE_FAIL"
        out["error"] = str(e)
    except socket.timeout:
        out["status"] = "TIMEOUT"
        out["error"] = "tls handshake timeout"
    except Exception as e:  # noqa
        out["error"] = str(e)
    return out


# ---------------------------------------------------------------- 各层诊断

def layer_dns(quick=False):
    """DNS 层: 对比系统解析器与权威公共 DNS"""
    results = []
    for name in ["github.com", "api.github.com", "ssh.github.com",
                 "codeload.github.com", "raw.githubusercontent.com"]:
        sys_ips = resolve_system(name)
        auth_map = {}
        if not quick:
            for srv in AUTHORITATIVE_DNS:
                ips = resolve_authoritative(name, srv)
                if ips:
                    auth_map[srv] = ips
        results.append({"name": name, "system": sys_ips or [], "auth": auth_map})
    return results


def judge_dns(entry):
    """
    判定:
      - 系统解析失败            -> DNS_FAIL
      - 系统 IP 与权威 DNS 无交集 -> SUSPECT_POISON (强污染信号)
      - 解析到 0.0.0.0/127.x/保留地址 -> SUSPECT_POISON
      - 一致                    -> OK
    """
    sys_ips = entry["system"]
    auth = entry["auth"]
    if not sys_ips:
        return "DNS_FAIL", "系统解析器无法解析"
    for ip in sys_ips:
        if ip.startswith(("0.", "127.", "10.", "169.254.")) or ip == "0.0.0.0":
            return "SUSPECT_POISON", "解析到非公网地址 %s" % ip
    if auth:
        auth_all = set()
        for ips in auth.values():
            auth_all |= set(ips)
        if auth_all and not (set(sys_ips) & auth_all):
            return "SUSPECT_POISON", "系统=%s 与权威=%s 无交集" % (
                ",".join(sys_ips), ",".join(sorted(auth_all)))
        return "OK", "与权威 DNS 一致"
    return "UNKNOWN", "无权威对照(quick 模式跳过)"


# ---------------------------------------------------------------- 应用层

def app_git_https(repo_url="https://github.com/carl800-1/spms.git"):
    """git over HTTPS 实测。返回 (status, detail)"""
    env = dict(os.environ)
    # 清掉本机代理变量, 测真实直连能力
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    try:
        p = subprocess.run(
            ["git", "-c", "http.proxy=", "-c", "https.proxy=",
             "ls-remote", repo_url, "HEAD"],
            capture_output=True, text=True, timeout=25,
            env=env, encoding="utf-8", errors="replace",
        )
        if p.returncode == 0 and p.stdout.strip():
            return "OK", p.stdout.strip().splitlines()[0]
        return "FAIL", (p.stderr or "").strip()[:160]
    except subprocess.TimeoutExpired:
        return "TIMEOUT", ">25s"
    except FileNotFoundError:
        return "NOT_FOUND", "git 未安装"
    except Exception as e:  # noqa
        return "ERR", str(e)


def app_git_ssh(repo_url="git@github.com:carl800-1/spms.git", port=None):
    """git over SSH 实测"""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    if port:
        env["GIT_SSH_COMMAND"] += " -p %d" % port
        repo_url = repo_url.replace("git@github.com:", "git@ssh.github.com:")
    try:
        p = subprocess.run(
            ["git", "ls-remote", repo_url, "HEAD"],
            capture_output=True, text=True, timeout=25,
            env=env, encoding="utf-8", errors="replace",
        )
        if p.returncode == 0 and p.stdout.strip():
            return "OK", p.stdout.strip().splitlines()[0]
        return "FAIL", (p.stderr or "").strip()[:160]
    except subprocess.TimeoutExpired:
        return "TIMEOUT", ">25s"
    except FileNotFoundError:
        return "NOT_FOUND", "git 未安装"
    except Exception as e:  # noqa
        return "ERR", str(e)


def app_ssh_handshake(host, port=22):
    """ssh 认证握手测试"""
    if not shutil.which("ssh"):
        return "NOT_FOUND", "ssh 客户端缺失"
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
           "-o", "StrictHostKeyChecking=accept-new", "-T", "-p", str(port),
           "git@" + host]
    rc, out = run_cmd(cmd, timeout=14)
    if "successfully authenticated" in out:
        return "OK", out.strip().splitlines()[-1] if out.strip() else "authenticated"
    if rc == -2:
        return "TIMEOUT", ">14s"
    return "FAIL", out.strip()[:160]


def app_http_get(url, timeout=12, samples=1):
    """纯 HTTP(S) 请求测试(用 ssl 模块,不依赖 requests)。支持多次采样。"""
    from urllib.request import Request, urlopen
    def once():
        try:
            req = Request(url, headers={"User-Agent": "ghdoctor/1.0"})
            ctx = ssl.create_default_context()
            t0 = time.time()
            with urlopen(req, timeout=timeout, context=ctx) as r:
                return "OK", "HTTP %d, %.2fs" % (r.status, time.time() - t0)
        except Exception as e:  # noqa
            msg = str(e)[:120]
            if "timed out" in msg.lower():
                return "TIMEOUT", msg
            return "FAIL", msg
    if samples <= 1:
        return once()
    got = [once() for _ in range(samples)]
    oks = [g for g in got if g[0] == "OK"]
    if len(oks) == samples:
        return "OK", oks[0][1]
    if oks:
        return "FLAKY", "%d/%d 成功" % (len(oks), samples)
    return got[0]


# ---------------------------------------------------------------- 代理/环境探测

def detect_proxy_env():
    keys = ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
            "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"]
    val = {}
    for k in keys:
        v = os.environ.get(k)
        if v:
            val[k] = v
    return val


def detect_git_proxy():
    rc, out = run_cmd(["git", "config", "--global", "--get-regexp",
                       "proxy|url\\."], timeout=6)
    return out.strip() if rc == 0 and out.strip() else ""


# ---------------------------------------------------------------- 报告输出

STATUS_MARK = {
    "OK": ok("[OK]"),
    "FLAKY": warn("[FLAKY]"),
    "FAIL": bad("[FAIL]"),
    "TIMEOUT": bad("[TIMEOUT]"),
    "RESET": bad("[RESET]"),
    "REFUSED": bad("[REFUSED]"),
    "DNS_FAIL": bad("[DNS-FAIL]"),
    "NOT_FOUND": warn("[MISSING]"),
    "CERT_MISMATCH": bad("[CERT-ALERT]"),
    "HANDSHAKE_FAIL": bad("[TLS-FAIL]"),
    "SUSPECT_POISON": bad("[SUSPECT-POISON]"),
    "UNKNOWN": warn("[?]"),
    "ERR": warn("[ERR]"),
}


def mark(s):
    return STATUS_MARK.get(s, warn("[%s]" % s))


def print_header(title):
    print("\n" + bold("=" * 68))
    print(bold("  " + title))
    print(bold("=" * 68))


def print_sub(title):
    print("\n" + bold("-- " + title + " " + "-" * max(0, 60 - len(title))))


def cmd_full(args):
    p = plat()
    report = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": p,
        "python": sys.version.split()[0],
        "layers": {},
    }

    print_header("GitHub 网络访问分层诊断  |  %s  |  平台=%s" %
                 (report["time"], p))

    # ---------- 代理环境 ----------
    print_sub("0. 本机代理环境")
    penv = detect_proxy_env()
    if penv:
        for k, v in penv.items():
            print("   %s = %s" % (k, v))
    else:
        print(dim("   (未设置代理环境变量)"))
    gpxy = detect_git_proxy()
    print("   git 全局代理: %s" % (gpxy if gpxy else dim("(无)")))
    report["layers"]["proxy_env"] = penv
    report["layers"]["git_proxy"] = gpxy

    # ---------- L1 DNS ----------
    print_sub("1. DNS 解析层")
    dns = layer_dns(quick=args.quick)
    report["layers"]["dns"] = []
    for e in dns:
        st, why = judge_dns(e)
        print("   %s %-28s sys=%s" % (mark(st), e["name"],
                                      ",".join(e["system"]) or "-"))
        if e["auth"]:
            for srv, ips in e["auth"].items():
                print(dim("        auth %-15s -> %s" % (srv, ",".join(ips))))
        if st != "OK":
            print("        %s %s" % (warn("判定:"), why))
        report["layers"]["dns"].append(
            {"name": e["name"], "verdict": st, "why": why,
             "system": e["system"], "auth": e["auth"]})

    # ---------- L2 连通 ----------
    print_sub("2. 网络连通层 (TCP)" + ("  [采样 x%d]" % args.samples if args.samples > 1 else ""))
    conn = []
    for host, port, proto, desc in TARGETS:
        st, detail = tcp_connect(host, port,
                                 timeout=3.0 if args.quick else DEFAULT_TIMEOUT,
                                 samples=args.samples)
        print("   %s %s:%d  %-34s %s" % (mark(st), host, port, desc, dim(detail)))
        conn.append({"host": host, "port": port, "status": st,
                     "detail": detail, "desc": desc})
    report["layers"]["connectivity"] = conn

    # ---------- L3 TLS ----------
    print_sub("3. TLS / 证书层")
    tls = []
    if not args.quick:
        for host in ["github.com", "api.github.com", "codeload.github.com",
                     "ssh.github.com"]:
            r = tls_inspect(host)
            print("   %s %-24s issuer=%s  ver=%s" %
                  (mark(r["status"]), host, r["issuer"] or "-", r["version"] or "-"))
            if r["status"] != "OK" and r["error"]:
                print(dim("        %s" % r["error"][:150]))
            if r.get("mismatch"):
                print("        %s CN=%s 与期望不符 -> 极可能中间人劫持" %
                      (bad("判定:"), r["subject"]))
            if r["issuer"] and not any(k in r["issuer"] for k in KNOWN_LEGIT_ISSUERS):
                print("        %s 签发者 %s 非主流 CA, 请核验" %
                      (warn("注意:"), r["issuer"]))
            tls.append(r)
    else:
        print(dim("   (--quick 跳过)"))
    report["layers"]["tls"] = tls

    # ---------- L4 应用层 ----------
    print_sub("4. 应用层通道")
    g_https = app_git_https()
    print("   %s git over HTTPS (443)        %s" % (mark(g_https[0]), dim(g_https[1])))
    g_ssh = app_git_ssh()
    print("   %s git over SSH   (22)         %s" % (mark(g_ssh[0]), dim(g_ssh[1])))
    g_ssh443 = app_git_ssh(port=443)
    print("   %s git over SSH   (443)        %s" % (mark(g_ssh443[0]), dim(g_ssh443[1])))
    dns_only_note = None

    api_ch = app_http_get("https://api.github.com", samples=args.samples)
    print("   %s HTTPS api.github.com        %s" % (mark(api_ch[0]), dim(api_ch[1])))
    raw_ch = app_http_get("https://raw.githubusercontent.com/torvalds/linux/master/README",
                          samples=args.samples)
    print("   %s HTTPS raw.githubusercontent %s" % (mark(raw_ch[0]), dim(raw_ch[1])))

    report["layers"]["application"] = {
        "git_https_443": g_https, "git_ssh_22": g_ssh,
        "git_ssh_443": g_ssh443, "https_api": api_ch, "https_raw": raw_ch,
    }

    # ---------- 结论 ----------
    print_header("诊断结论与可用通道")
    usable = []
    if g_ssh[0] == "OK":
        usable.append("SSH:22")
    if g_ssh443[0] == "OK":
        usable.append("SSH:443")
    if g_https[0] == "OK":
        usable.append("HTTPS:443")
    if "SSH:22" not in usable and "SSH:443" not in usable:
        # 通道级失败时, 看 TCP 是否只是握手超时
        pass

    if usable:
        print("   可用通道: %s" % ok(", ".join(usable)))
    else:
        print("   可用通道: %s" % bad("无 — 需按下方治理方案处理"))

    # 各层判定
    dns_bad = [d for d in report["layers"]["dns"] if d["verdict"] == "SUSPECT_POISON"]
    dns_fail = [d for d in report["layers"]["dns"] if d["verdict"] == "DNS_FAIL"]
    cert_bad = [t for t in tls if t["status"] in ("CERT_MISMATCH",)]
    tcp443 = next((c for c in conn if c["host"] == "github.com" and c["port"] == 443), None)

    print()
    if dns_bad or dns_fail:
        print("   %s DNS 层异常 -> 优先治理: 加密 DNS(DOH/DOT) 或 hosts 绑定" % bad("▲"))
    else:
        print("   %s DNS 层正常, 解析结果可信" % ok("✓"))
    if cert_bad:
        print("   %s TLS 层异常 -> 疑似中间人劫持, 只信系统根证书并核对指纹" % bad("▲"))
    else:
        print("   %s TLS 层正常, 无劫持特征" % ok("✓"))
    if tcp443 and tcp443["status"] in ("TIMEOUT", "RESET"):
        print("   %s 443 端口被阻断 -> 走 SSH(22 或 443) 或代理" % bad("▲"))
    elif usable:
        print("   %s 连通层有可用路径" % ok("✓"))

    # 治理建议
    print_sub("建议的治理顺序")
    for i, s in enumerate(suggest_remediation(report), 1):
        print("   %d) %s" % (i, s))

    report["verdict"] = {
        "usable_channels": usable,
        "dns_poisoned": bool(dns_bad or dns_fail),
        "tls_hijacked": bool(cert_bad),
        "port_443_blocked": bool(tcp443 and tcp443["status"] in ("TIMEOUT", "RESET")),
        "recommendations": suggest_remediation(report),
    }

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print("\n   " + dim("已写出 JSON: %s" % args.json))

    return 0 if usable else 2


def suggest_remediation(report):
    """根据诊断结果生成治理建议(有序, 含生效范围/成本)"""
    v = report.get("verdict", {})
    r = []
    if v.get("dns_poisoned"):
        r.append("DNS 污染 -> 启用加密 DNS: Windows DoH / macOS 描述文件 / systemd-resolved DoT")
    r.append("首选低风险方案: 把 remote 切到 SSH 443 -> ssh.github.com:443 (穿越封锁, 无需 root)")
    if v.get("dns_poisoned"):
        r.append("次选: hosts 绑定 GitHub 真实 IP (需自行维护, IP 会漂移)")
    r.append("代理方案: 确保 git 显式走可用代理 (git config --global http.proxy)")
    if v.get("tls_hijacked"):
        r.append("若证书告警: 检查系统根证书库与杀软 HTTPS 扫描, 切勿关闭证书校验")
    r.append("镜像兜底: 对只读依赖可使用 ghproxy / 国内镜像 (仅限拉取)")
    return r


# ---------------------------------------------------------------- 子命令

def cmd_channels(args):
    print_header("可用通道快速检测")
    results = {}
    g22 = app_ssh_handshake("github.com", 22)
    print("   SSH github.com:22       %s %s" % (mark(g22[0]), dim(g22[1])))
    results["ssh22"] = g22
    g443 = app_ssh_handshake("ssh.github.com", 443)
    print("   SSH ssh.github.com:443  %s %s" % (mark(g443[0]), dim(g443[1])))
    results["ssh443"] = g443
    h = app_http_get("https://github.com", timeout=10)
    print("   HTTPS github.com:443    %s %s" % (mark(h[0]), dim(h[1])))
    results["https"] = h
    best = None
    if g443[0] == "OK":
        best = "ssh.github.com:443"
    elif g22[0] == "OK":
        best = "github.com:22"
    elif h[0] == "OK":
        best = "https://github.com"
    print("\n   推荐通道: %s" % (ok(best) if best else bad("无")))
    print("   配置命令: %s" % dim("git config --global url.\\\"git@ssh.github.com:443\\\".insteadOf \\\"git@github.com:\\\""))
    return 0 if best else 2


def cmd_fix_hosts(args):
    print_header("hosts 绑定片段生成 (仅生成, 不写入)")
    pairs = []
    for name in ["github.com", "api.github.com", "codeload.github.com",
                 "raw.githubusercontent.com", "objects.githubusercontent.com"]:
        ips = resolve_authoritative(name, "1.1.1.1") or resolve_authoritative(name, "8.8.8.8")
        if ips:
            pairs.append((ips[0], name))
    if not pairs:
        print(bad("   无法从权威 DNS 获取 IP, 请检查网络"))
        return 2
    print("\n" + dim("   # ---- GitHub hosts 片段 (复制到 hosts 文件) ----"))
    for ip, name in pairs:
        print("   %-18s %s" % (ip, name))
    hp = hosts_path()
    print("\n   hosts 路径: %s" % hp)
    print("   备份命令:   %s" % dim(backup_hosts_cmd(hp)))
    print("   刷新缓存:   %s" % dim(flush_dns_cmd()))
    print("\n   " + warn("注意: GitHub IP 会漂移, 建议每 1-3 个月重跑本命令核对"))
    return 0


def hosts_path():
    p = plat()
    if p == "win":
        return r"C:\Windows\System32\drivers\etc\hosts"
    return "/etc/hosts"


def backup_hosts_cmd(hp):
    p = plat()
    if p == "win":
        return 'copy "%s" "%s.bak.%%date:~0,10%%"' % (hp, hp)
    return "sudo cp %s %s.bak.$(date +%%F)" % (hp, hp)


def flush_dns_cmd():
    p = plat()
    if p == "win":
        return "ipconfig /flushdns"
    if p == "mac":
        return "sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder"
    return "sudo systemctl restart systemd-resolved   # 或 resolvectl flush-caches"


def cmd_update_ips(args):
    print_header("GitHub IP 重新获取与对比")
    names = ["github.com", "api.github.com", "codeload.github.com",
             "raw.githubusercontent.com"]
    changed = False
    for name in names:
        sys_ips = resolve_system(name) or []
        auth = set()
        for srv in AUTHORITATIVE_DNS[:2]:
            v = resolve_authoritative(name, srv)
            if v:
                auth |= set(v)
        status = "OK"
        if not sys_ips:
            status = "DNS_FAIL"
        elif auth and not (set(sys_ips) & auth):
            status = "SUSPECT_POISON"
        print("   %s %-26s system=%s  auth=%s" %
              (mark(status), name,
               ",".join(sys_ips) or "-", ",".join(sorted(auth)) or "-"))
        if auth and not (set(sys_ips) & auth):
            changed = True
    print()
    if changed:
        print("   %s 检测到解析不一致, 建议重新生成 hosts 片段:" % warn("!"))
        print(dim("       python %s --fix-hosts" % os.path.basename(sys.argv[0])))
    else:
        print("   %s 解析正常, 无需更新" % ok("✓"))
    return 0


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser(
        description="GitHub 网络访问分层诊断与治理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("--quick", action="store_true", help="快速模式(跳过 TLS 与权威 DNS 对比)")
    ap.add_argument("--samples", type=int, default=1, metavar="N",
                    help="TCP 连通性采样次数(识别间歇性阻断), 建议 3-5")
    ap.add_argument("--json", metavar="FILE", help="输出 JSON 结果到文件")
    ap.add_argument("--channels", action="store_true", help="只检测可用通道")
    ap.add_argument("--fix-hosts", action="store_true", help="生成 hosts 修复片段(不写入)")
    ap.add_argument("--update-ips", action="store_true", help="重新获取并对比 GitHub IP")
    args = ap.parse_args()

    if args.channels:
        return cmd_channels(args)
    if args.fix_hosts:
        return cmd_fix_hosts(args)
    if args.update_ips:
        return cmd_update_ips(args)
    return cmd_full(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中断")
        sys.exit(3)
