# frp 内网穿透新增服务指南

> 基于真实踩坑经验编写，确保少走弯路
> 目标：新增一个本地服务到公网访问，耗时 < 10 分钟

---

## 一、先决条件

### 1.1 确认 frp 环境已就绪

```bash
# 确认 frpc 在运行
ps aux | grep frpc | grep -v grep

# 确认 frps 在云服务器运行
ssh root@8.162.3.212 'ps aux | grep frps | grep -v grep'

# 确认云服务器已开放 443（HTTPS via HAProxy）
ssh root@8.162.3.212 'ss -tlnp | grep 443'

# 确认 HAProxy 在跑
ssh root@8.162.3.212 'ps aux | grep haproxy | grep -v grep'
```

**踩坑记录：**
- ⚠️ frpc 可能开了但只加载了部分代理，旧配置重启后未生效 → 每次修改 frpc.toml 后必须重启 frpc
- ⚠️ frps 如果停了，所有隧道都会断 → 先检查 `ps aux | grep frps`
- ⚠️ 安全组里只开了 8443 不够，必须同步开放 443（HTTPS）和对应的 frpc 端口
- ⚠️ HAProxy 如果停了，443 端口会空出来，所有 HTTPS 服务会报连接被拒

---

## 二、新增 HTTP 服务到公网

假设新服务在本地 `localhost:5005` 运行，协议是 HTTP。

### 步骤 1：本地启动新服务

```bash
cd /path/to/new-project
python3 app.py   # 或 ./start.sh
```

### 步骤 2：确认本地端口正常

```bash
curl -s http://127.0.0.1:5005/ | head -3
# 应该返回 HTML 或 JSON，不是 connection refused
```

### 步骤 3：选择一个未被占用的公网端口

```bash
ssh root@8.162.3.212 'ss -tlnp | grep -E "6001|6004|6005|6006|2222|7000"'
```

**常用端口规划：**

| 端口 | 用途 |
|------|------|
| 6001 | Whisper Web HTTP |
| 6004 | TranslateTraining |
| 6005 | 新服务（推荐） |
| 6006 | 备用 |
| 2222 | SSH 回程 |
| 7000 | frps 控制端口 |

### 步骤 4：修改 frpc.toml

```bash
nano ~/.config/frp/frpc.toml
```

添加一个 `[[proxies]]` 块：

```toml
[[proxies]]
name = "new-service"      # 起个有意义的名字，不能和其他代理重名
type = "tcp"
localIP = "127.0.0.1"
localPort = 5005          # 新服务的本地端口
remotePort = 6005         # 公网端口，选择一个未被占用的
```

**踩坑记录：**
- ⚠️ `name` 不能重复，frpc 加载时报错但不会说具体哪个重复了
- ⚠️ `localPort` 填错了隧道通了也没用，先用 curl 确认本地端口
- ⚠️ `remotePort` 填了已被占用的端口，frps 会报端口冲突

### 步骤 5：开放云服务器安全组

> **最容易漏掉的一步。**

登录阿里云控制台 → ECS → 安全组 → 入方向规则，添加：

| 协议 | 端口范围 | 来源 |
|------|---------|------|
| TCP | 6005/6005 | 0.0.0.0/0 |

**踩坑记录：**
- ⚠️ 只开 iptables 不够，阿里云安全组是另一层，必须同步开放
- ⚠️ 安全组生效可能有几分钟延迟，改完不要立刻测试

### 步骤 6：云服务器加 iptables 规则

```bash
ssh root@8.162.3.212 'iptables -I INPUT -p tcp --dport 6005 -j ACCEPT'
```

**踩坑记录：**
- ⚠️ 云服务器重启后 iptables 规则会丢失，需要持久化（`iptables-save`）

### 步骤 7：重启 frpc

```bash
# 先杀掉所有 frpc 进程（可能有多个旧进程占着连接）
pkill -9 -f "frpc -c /Users/jing/.config/frp/frpc.toml"
sleep 2

# 重新启动
/opt/homebrew/opt/frpc/bin/frpc -c /Users/jing/.config/frp/frpc.toml > /tmp/frpc.log 2>&1 &
sleep 5

# 检查日志确认所有代理都成功
cat /tmp/frpc.log | grep "start proxy success"
```

确认日志里看到：

```
[ssh] start proxy success
[web] start proxy success
[translate-tcp] start proxy success
[native-speaking] start proxy success
[aitalk-tcp] start proxy success
[new-service] start proxy success
```

**踩坑记录：**
- ⚠️ frpc 不支持热更新，改了配置必须重启
- ⚠️ 杀进程后必须等 2 秒再启动，避免端口占用冲突
- ⚠️ 如果有多个 frpc 进程，旧的那个占着 frps 连接，新配置不会生效 → 用 `pkill -9` 强制杀光
- ⚠️ `web-https` 代理已废弃（frps 原生 HTTPS 方案），HTTPS 改由 HAProxy SNI 路由

### 步骤 8：验证

```bash
# 本地
curl -s http://127.0.0.1:5005/ | head -3

# 公网
curl -s --connect-timeout 8 http://8.162.3.212:6005/ | head -3
```

---

## 三、新增 HTTPS 服务到公网

### 方案对比：frps 原生 vs HAProxy SNI 路由

| | 方案 A：frps 原生 HTTPS | 方案 B：HAProxy SNI 路由（已采用）|
|---|---|---|
| 云服务器组件 | frps | HAProxy |
| HTTPS 端口 | 8443（单一端口靠 Host 头区分） | 443（单一端口靠 SNI 区分） |
| 多域名 HTTPS | ❌ 同一证书，frps 0.58.1 不支持 SNI | ✅ HAProxy 原生支持多证书 + SNI |
| 本地服务要求 | 必须监听 HTTPS（:5002 等） | HTTP 或 HTTPS 均可 |
| 证书存放 | 本地服务内嵌 | 云服务器 |
| 手机浏览器支持 | ✅（需忽略证书警告） | ✅（需忽略证书警告） |
| **适合场景** | 单一 HTTPS 服务 | **多 HTTPS 服务共用 443**（当前采用） |

**当前架构（方案 B）：**

```
浏览器 ──https──▶ 8.162.3.212:443 (HAProxy)
                        │
                        │ SNI 路由（ssl_fc_sni）
                        ▼
              ┌─────────┴──────────┐
              │                    │
   SNI=whisper.*           SNI=aitalk.*
              │                    │
              ▼                    ▼
         :6001 (frpc)        :8001 (socat)
              │                    │
              ▼                    ▼
        Mac :5003 (HTTP)     Mac :8000 (HTTP)
        Whisper Web           AITalk
```

**核心原理：** HAProxy 在 TLS 握手阶段读取 SNI（Server Name Indication）扩展字段，根据域名选择对应 backend，无需在云服务器运行 HTTPS 服务。

### 前提条件

1. **HAProxy 已安装并运行在云服务器 443 端口**（已安装）
2. **阿里云安全组已开放 443**（已开放）
3. **socat 已安装**（用于 HTTP 转发：`yum install -y socat`）
4. **新服务的证书 CN 或 SAN 包含访问域名**（mkcert 生成）

### 步骤 1：确认本地 HTTP 服务正常

```bash
curl -s http://127.0.0.1:5006/ | head -3
# 返回 200 说明服务本身正常
```

> **不需要本地 HTTPS 代理。** 新方案中云服务器 HAProxy 直接连接 Mac 的 HTTP 端口，浏览器到云服务器走 HTTPS，云服务器到 Mac 走 HTTP（内网，安全）。

### 步骤 2：获取访问域名和生成证书

访问域名使用 **nip.io 泛解析**，格式：`{子域名}.8.162.3.212.nip.io`

例如新服务叫 `chat`，访问域名就是 `chat.8.162.3.212.nip.io`。

```bash
# 在 Mac 本地生成证书（需要 mkcert）
cd ~/Project/your-project
mkcert chat.8.162.3.212.nip.io localhost 127.0.0.1
# 生成：
#   chat.8.162.3.212.nip.io.pem
#   chat.8.162.3.212.nip.io-key.pem
```

### 步骤 3：确认证书包含访问域名

```bash
openssl x509 -in ./chat.8.162.3.212.nip.io.pem -text -noout | grep -E "DNS:|IP Address:"
# 应该看到 chat.8.162.3.212.nip.io
```

**踩坑记录：**
- ⚠️ 证书的 CN/SAN 必须包含访问域名，否则浏览器会报证书错误（不是警告，是直接拒绝）
- ⚠️ 域名格式：`<子域名>.8.162.3.212.nip.io`，和 IP 地址之间用 `-` 连接，不是 `.`

### 步骤 4：在云服务器添加 socat 转发

如果新服务走 HTTP（不需要 Mac 本地 HTTPS 代理），需要在云服务器用 socat 把 HAProxy 的流量转发到 Mac 的 HTTP 端口：

```bash
ssh root@8.162.3.212
# 查看已有哪些 socat 进程
ps aux | grep socat | grep -v grep
# 新增一条 socat（例如新服务本地端口是 8080）
nohup socat TCP-LISTEN:8002,fork,reuseaddr TCP:127.0.0.1:6006 >/dev/null 2>&1 &
```

> **原理：** socat 在云服务器监听一个新端口（如 8002），转发给 frpc 的 HTTP 端口（如 6006）。HAProxy 的 backend 指向 `127.0.0.1:8002`。这样 HTTPS 终止在云服务器，HTTP 流量通过 socat → frpc → Mac。

**踩坑记录：**
- ⚠️ socat 进程重启后会丢失，需要加入 crontab `@reboot` 确保开机自启：
  ```bash
  ssh root@8.162.3.212 "(crontab -l 2>/dev/null | grep -v socat; echo '@reboot nohup socat TCP-LISTEN:8002,fork,reuseaddr TCP:127.0.0.1:6006 >/dev/null 2>&1 &') | crontab -"
  ```

### 步骤 5：修改 frpc.toml

```bash
nano ~/.config/frp/frpc.toml
```

添加 HTTP 代理块：

```toml
[[proxies]]
name = "new-service-http"   # 起个不重复的名字
type = "tcp"
localIP = "127.0.0.1"
localPort = 5006             # Mac 本地 HTTP 端口
remotePort = 6006             # 公网 HTTP 端口（HAProxy 通过 socat 访问这个端口）
```

### 步骤 6：更新 HAProxy 配置（SNI 路由）

```bash
ssh root@8.162.3.212
sudo nano /etc/haproxy/haproxy.cfg
```

在 `frontend https_front` 块中添加新路由：

```
use_backend chat_backend if { ssl_fc_sni -i chat.8.162.3.212.nip.io }
```

在文件末尾添加新的 backend：

```
backend chat_backend
    mode tcp
    server chat 127.0.0.1:8002
```

**完整的 HAProxy 配置结构：**

```
global
    log stdout format raw local0
    maxconn 4096

defaults
    log global
    mode tcp
    option tcplog
    timeout client 300000
    timeout server 300000
    timeout connect 5000

frontend https_front
    bind *:443 ssl crt-list /etc/haproxy/cert_list.txt
    mode tcp
    tcp-request inspect-delay 3s

    use_backend whisper_backend if { ssl_fc_sni -i whisper.8.162.3.212.nip.io }
    use_backend aitalk_backend  if { ssl_fc_sni -i aitalk.8.162.3.212.nip.io }
    use_backend chat_backend    if { ssl_fc_sni -i chat.8.162.3.212.nip.io }   # ← 新增
    default_backend whisper_backend

backend whisper_backend
    mode tcp
    server whisper 127.0.0.1:6001

backend aitalk_backend
    mode tcp
    server aitalk 127.0.0.1:8001

backend chat_backend                # ← 新增
    mode tcp
    server chat 127.0.0.1:8002
```

### 步骤 7：将证书加入 cert_list.txt

```bash
ssh root@8.162.3.212
# 合并 cert + key
cat /path/on/mac/chat.8.162.3.212.nip.io.pem /path/on/mac/chat.8.162.3.212.nip.io-key.pem \
  > /etc/haproxy/certs/chat-combined.pem

# 追加到证书列表
echo "/etc/haproxy/certs/chat-combined.pem" >> /etc/haproxy/cert_list.txt
cat /etc/haproxy/cert_list.txt
```

**踩坑记录：**
- ⚠️ HAProxy 按证书列表顺序匹配：第一个匹配的证书会被使用。所以要在 cert_list.txt 中**把新证书加在最前面**，避免被已有的默认证书抢先匹配
- ⚠️ cert_list.txt 格式：每个证书路径一行，证书必须是 cert + key 合并的 PEM 文件

### 步骤 8：开放云服务器安全组

阿里云控制台 → ECS → 安全组 → 入方向规则，添加：

| 协议 | 端口范围 | 来源 |
|------|---------|------|
| TCP | 6006/6006 | 0.0.0.0/0 | ← frpc HTTP 代理端口 |
| TCP | 8002/8002 | 127.0.0.1 | ← socat 内部端口（仅本地） |

> 注：443 已在 HAProxy 使用，无需额外开放。

### 步骤 9：重启 frpc 和 HAProxy

```bash
# 重启 frpc
pkill -9 -f "frpc -c /Users/jing/.config/frp/frpc.toml"
sleep 2
/opt/homebrew/opt/frpc/bin/frpc -c /Users/jing/.config/frp/frpc.toml > /tmp/frpc.log 2>&1 &
sleep 5
cat /tmp/frpc.log | grep "start proxy success"

# 重启 HAProxy
ssh root@8.162.3.212 "killall -9 haproxy 2>/dev/null; sleep 2; haproxy -f /etc/haproxy/haproxy.cfg"
```

### 步骤 10：验证

```bash
# HTTPS 访问（-k 忽略证书警告）
curl -sk --connect-timeout 8 https://chat.8.162.3.212.nip.io/ | head -3
# 返回页面内容说明成功

# 从云服务器本地验证路由
ssh root@8.162.3.212 "curl -sk https://chat.8.162.3.212.nip.io/ | grep -o '<title>[^<]*</title>'"
```

浏览器访问：`https://chat.8.162.3.212.nip.io/`

首次访问显示"此连接不安全"警告 → 点击"继续前往网站"（Chrome）或"访问此网站"（Safari）。

### 踩坑记录（新增 HTTPS 服务必读）

- ⚠️ **HAProxy SNI 变量用 `ssl_fc_sni` 而不是 `req.ssl_sni`**
  - HAProxy 2.8 中，TLS SNI 字段的正确变量名是 `ssl_fc_sni`（frontend connection 侧）
  - `req.ssl_sni` 在这个版本返回空值，会导致所有路由都落到 `default_backend`
- ⚠️ **证书文件必须是 cert + key 合并的 PEM**：`cat cert.pem key.pem > combined.pem`
- ⚠️ **cert_list.txt 中新证书要加在最前面**，否则可能被前面的证书抢先匹配
- ⚠️ **socat 进程不会开机自启**，必须加 crontab `@reboot`
- ⚠️ **HAProxy 重启后配置生效但不会通知**，用 `ss -tlnp | grep 443` 确认 443 端口被 HAProxy 持有
- ⚠️ **如果 HAProxy 启动失败（配置错误），443 端口会空出来**，此时浏览器访问 HTTPS 会报连接被拒

---

## 四、隧道架构图（当前配置）

```
Mac 本地 (.config/frp/frpc.toml)
┌──────────────────────────────────────────────────────────┐
│  serverAddr = "8.162.3.212"                             │
│  serverPort = 7000                                       │
│                                                           │
│  [[proxies]]                                             │
│  ssh              :22   → :2222  (TCP)                  │
│  web              :5003 → :6001  (TCP/HTTP)              │
│  translate-tcp    :5004 → :6004  (TCP)                   │
│  native-speaking  :3000 → :6005  (TCP)                   │
│  aitalk           :8000 → :6002  (TCP/HTTP)  ← AITalk   │
└──────────────────────────────────────────────────────────┘
          │
          │ frpc ↔ frps 加密隧道 (port 7000)
          ▼
云服务器 8.162.3.212
┌──────────────────────────────────────────────────────────┐
│                                                           │
│  frps 0.58.1  ── TCP 隧道 ──► Mac 本地端口             │
│  :6001 → Mac :5003 (Whisper)                            │
│  :6002 → Mac :8000 (AITalk)                             │
│  :6004 → Mac :5004 (Translate)                          │
│  :6005 → Mac :3000 (NativeSpeaking)                     │
│                                                           │
│  HAProxy 2.8  ── HTTPS SNI 路由 ──► frpc / socat       │
│  :443 (HTTPS)                                            │
│     ├─ SNI=whisper.*  → 6001 → Mac :5003 (Whisper)    │
│     └─ SNI=aitalk.*   → 8001 (socat) → 6002 → :8000   │
│                                                           │
│  socat ── 转发 ──► frpc                                  │
│  :8001 → 127.0.0.1:6002 (AITalk)                        │
│                                                           │
│  ┌─ 阿里云安全组 (控制台) ───────────────────────────┐   │
│  │  TCP 443, 2222, 7000, 6001, 6002, 6004, 6005  │   │
│  └──────────────────────────────────────────────────┘   │
│                                                           │
│  ⚠ 安全组里没有 8443（已废弃）                         │
└──────────────────────────────────────────────────────────┘
          │
          │ 公网
          ▼
  ┌─────────────────────────────────────────┐
  │  HTTPS（HAProxy SNI 路由）               │
  │  https://whisper.8.162.3.212.nip.io/       │
  │  https://aitalk.8.162.3.212.nip.io/        │
  │                                          │
  │  HTTP（frpc TCP 直连）                   │
  │  http://8.162.3.212:6001/  → Whisper    │
  │  http://8.162.3.212:6002/  → AITalk     │
  │  http://8.162.3.212:6004/  → Translate   │
  │  http://8.162.3.212:6005/  → NativeSpk  │
  └─────────────────────────────────────────┘
```

### 当前 HAProxy 配置

配置文件：`/etc/haproxy/haproxy.cfg`

```
frontend https_front (bind :443 ssl)
  ├─ ssl_fc_sni == whisper.*   → backend whisper_backend (:6001)
  ├─ ssl_fc_sni == aitalk.*    → backend aitalk_backend  (:8001/socat)
  └─ default                    → backend whisper_backend (:6001)

backend whisper_backend  → server whisper 127.0.0.1:6001
backend aitalk_backend   → server aitalk 127.0.0.1:8001
```

证书列表：`/etc/haproxy/cert_list.txt`（按顺序匹配）

```
/etc/haproxy/certs/whisper-combined.pem    ← whisper 证书
/etc/haproxy/certs/aitalk-combined.pem     ← aitalk 证书
```

云服务器开机自启：crontab `@reboot /usr/sbin/haproxy -f /etc/haproxy/haproxy.cfg`

---

## 五、排障清单（已更新 HTTPS 诊断）

| 顺序 | 检查项 | 命令 |
|------|--------|------|
| 1 | frpc 是否在跑 | `ps aux \| grep frpc \| grep -v grep` |
| 2 | frpc 有没有多个旧进程 | `ps aux \| grep frpc \| grep -v grep \| wc -l`（大于 1 说明有残留） |
| 3 | frpc 日志 | `cat /tmp/frpc.log \| grep -E "error\|success\|warn"` |
| 4 | frps 是否在跑 | `ssh root@8.162.3.212 'ps aux \| grep frps'` |
| 5 | **HAProxy 是否持有 443** | `ssh root@8.162.3.212 'ss -tlnp \| grep 443'`（必须是 haproxy，不是空） |
| 6 | **socat 转发是否正常** | `ssh root@8.162.3.212 'curl -s http://127.0.0.1:8001/ \| head -1'` |
| 7 | 本地端口是否正常 | `curl -s http://127.0.0.1:5005/` |
| 8 | 本地 HTTPS 端口是否正常 | `curl -sk https://127.0.0.1:5006/` |
| 9 | 证书是否包含访问域名 | `openssl x509 -in /path/cert.pem -text \| grep DNS` |
| 10 | 云服务器端口是否监听 | `ssh root@8.162.3.212 'ss -tlnp \| grep 6005'` |
| 11 | 阿里云安全组是否放行 | 控制台 → 安全组 → 入方向规则 |
| 12 | iptables 是否放行 | `ssh root@8.162.3.212 'iptables -L INPUT -n'` |
| 13 | 本地能否连接云服务器 | `nc -z -w5 8.162.3.212 7000` |
| 14 | HTTPS 域名解析 | `ping -c 1 chat.8.162.3.212.nip.io`（nip.io 应解析到 8.162.3.212） |
| 15 | **SNI 路由是否正确** | 从云服务器本地测试：`ssh root@8.162.3.212 'curl -sk https://aitalk.8.162.3.212.nip.io/ \| grep title'` |
| 16 | **HAProxy 日志** | `ssh root@8.162.3.212 'journalctl -u haproxy -n 20'` |

---

## 六、常用命令速查

### Mac 本地

```bash
# 重启 frpc（杀掉所有残留进程后启动）
pkill -9 -f "frpc -c /Users/jing/.config/frp/frpc.toml"
sleep 2
/opt/homebrew/opt/frpc/bin/frpc -c /Users/jing/.config/frp/frpc.toml > /tmp/frpc.log 2>&1 &
sleep 5
cat /tmp/frpc.log

# 查看 frpc 配置
cat ~/.config/frp/frpc.toml

# 查看本地端口占用
lsof -i :5002 -i :5003 -i :5004 -i :5005 -P -n | grep LISTEN
```

### 云服务器

```bash
# 重启 frps
ssh root@8.162.3.212 'pkill -f frps; sleep 1; cd /root/frp_0.58.1_linux_amd64 && nohup ./frps -c frps.toml > frps.log 2>&1 &'
sleep 2
ssh root@8.162.3.212 'ss -tlnp | grep -E "7000|6001|6002|6004|443"'

# 查看 frps 日志
ssh root@8.162.3.212 'tail -30 /root/frps.log'

# 重启 HAProxy（修改 haproxy.cfg 后）
ssh root@8.162.3.212 'killall -9 haproxy 2>/dev/null; sleep 2; haproxy -f /etc/haproxy/haproxy.cfg'
sleep 2
ssh root@8.162.3.212 'ss -tlnp | grep 443'   # 确认 HAProxy 持有 443

# 添加 iptables 规则
ssh root@8.162.3.212 'iptables -I INPUT -p tcp --dport 6005 -j ACCEPT'

# 查看 HAProxy 路由日志
ssh root@8.162.3.212 'tail -20 /var/log/syslog | grep haproxy'

# 新增 socat 转发（云服务器本地端口 → frpc 端口）
ssh root@8.162.3.212 'nohup socat TCP-LISTEN:8002,fork,reuseaddr TCP:127.0.0.1:6006 >/dev/null 2>&1 &'
```

### 验证连通性

```bash
# 本地 HTTP
curl -s http://127.0.0.1:5005/ | head -3

# 本地 HTTPS
curl -sk https://127.0.0.1:5006/ | head -3

# 公网 HTTP（frpc TCP）
curl -s --connect-timeout 8 http://8.162.3.212:6005/ | head -3

# 公网 HTTPS（HAProxy SNI 路由）-k 忽略证书警告
curl -sk --connect-timeout 8 https://chat.8.162.3.212.nip.io/ | head -3

# 从云服务器本地验证 HTTPS 路由（SNI 是否正确）
ssh root@8.162.3.212 'curl -sk https://aitalk.8.162.3.212.nip.io/ | grep -o "<title>[^<]*</title>"'
```

---

## 七、版本统一建议

| 组件 | 当前版本 | 状态 |
|------|---------|------|
| frpc (Mac) | 0.68.1 (homebrew) | ⚠️ 高版本，兼容性需注意 |
| frps (云服务器) | 0.58.1 | ⚠️ 低版本，部分新字段可能不支持 |
| HAProxy (云服务器) | 2.8.14 | ✅ 用于 HTTPS SNI 路由 |

**踩坑记录：**
- ⚠️ frps `vhostHTTPSPort` 已禁用（当前不用），HTTPS 路由改由 HAProxy 接管
- ⚠️ HAProxy 安装：`yum install -y haproxy`（alibaba linux 的 aa_nginx 源）
- ⚠️ Tengine（阿里云预装 nginx）和 HAProxy 不可同时监听 443，只能二选一，当前用 HAProxy
- ⚠️ 如果升级 frps，所有 frpc 连接会断开，所有服务同时中断，务必安排在维护窗口

---

## 八、常见错误速查（已更新）

| 错误信息 | 原因 | 解决方法 |
|---------|------|---------|
| `subdomain and custom domains should not be both empty` | `type = "https"` 但没填 `customDomains` | 添加 `customDomains = ["域名"]` |
| `proxy [xxx] already exists` | 有多个 frpc 进程在跑 | `pkill -9 -f frpc` 全部杀掉后重启 |
| `connection refused`（公网 HTTP） | frpc 没在跑 / 安全组没开 / iptables 没开 | 按排障清单顺序查 |
| `curl: (7) Failed to connect`（公网 HTTPS） | HAProxy 没跑 / 443 端口被其他进程占用 | `ssh root@8.162.3.212 'ss -tlnp \| grep 443'` 确认是 haproxy |
| `curl: (35) SSL handshake failed`（本地 curl 测试） | 云服务器 curl 请求未加 `-k` | 加 `-k` 忽略证书警告 |
| 页面标题是 Whisper 但访问的是 aitalk 子域名 | HAProxy SNI 路由失败，`ssl_fc_sni` 返回空值 | 确认 HAProxy 配置使用 `ssl_fc_sni` 而非 `req.ssl_sni` |
| HTTPS 页面样式全乱 | frp 代理 HTTP 端口但服务强制跳 HTTPS | 改用 HTTPS 方案：本地代理或 HAProxy SNI 路由 |
| AITalk 页面无限重定向 / 空白 | Mac 本地 `aitalk_https_proxy.py` 代理挂了 | `ps aux \| grep aitalk_https_proxy` 确认进程在跑，没有则重启 |
| HAProxy 启动报错 `unknown keyword 'xxx'` | HAProxy 2.8 配置语法较严格 | 查官方文档，确认关键字和语法格式 |
| `parse argument modifier without variable name` | log-format 里的 `%{+Q}` 语法不对 | 用简单 log-format 或直接去掉自定义日志格式 |

---

## 九、服务清单（当前配置）

| 服务名 | 本地端口 | 公网 HTTP | 公网 HTTPS | 协议 | 添加日期 | 备注 |
|--------|---------|-----------|-----------|------|---------|------|
| Whisper Web | 5003 | :6001 | whisper.* (HAProxy) | TCP | 2026-04-19 | |
| TranslateTraining | 5004 | :6004 | — | TCP | 2026-04-19 | |
| SSH 回程 | 22 | :2222 | — | TCP | 2026-04-19 | |
| NativeSpeaking | 3000 | :6005 | — | TCP | 2026-04-19 | |
| AITalk | 8000 | :6002 | aitalk.* (HAProxy) | TCP | 2026-04-19 | 本地 HTTP，云服务器 socat 转发 |
| [新服务] | 5005 | :6005 | — | TCP | YYYY-MM-DD | 描述 |
| [新 HTTPS 服务] | 5006 | :6006 | chat.* (HAProxy) | TCP | YYYY-MM-DD | 描述，chat.* 为示例 |
