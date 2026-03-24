# ali2tencent-cloud

阿里云（Alibaba Cloud Linux / ALinux 等）镜像自动迁移到腾讯云（CVM）的流水线工具：自动检测新版本 → 下载镜像 → 注入/准备 cloud-init 配置 → 上传 COS → ImportImage 导入自定义镜像 → 启动按量实例 → SSH 性能基准测试 → 生成对比报告。

## 你会得到什么

- 自动抓取阿里云镜像发布页，发现新版本并入库（SQLite）
- 支持断点续传的镜像下载 + SHA256 校验
- 面向腾讯云的 cloud-init 兼容性处理（尽可能自动注入；无法注入时会生成参考文件）
- COS 分片上传 + 生成 24h 预签名下载 URL（供 ImportImage 使用）
- CVM 镜像导入（轮询等待完成）+ 启动按量计费实例并获取公网 IP
- SSH 执行基准测试脚本（sysbench/fio/iperf3）并把结构化结果写入 SQLite
- 生成 HTML / JSON 报告（含最新 report_latest.html）

## 目录结构

- core/
  - config.py：从项目根目录 .env 读取配置
  - state.py：SQLite 状态库（版本、任务、基准测试结果）
  - scheduler.py：daemon 定时调度
  - logger.py：日志输出到控制台 + logs/pipeline.log
- pipeline/
  - pipeline.py：流水线编排（8 个阶段）
  - monitor.py：检测新版本（解析阿里云页面/目录列表）
  - downloader.py：下载镜像（断点续传、校验）
  - image_modifier.py：cloud-init 注入/准备（Windows 推荐 WSL 注入）
  - cos_uploader.py：上传 COS（分片）并生成预签名 URL
  - cvm_importer.py：ImportImage 导入自定义镜像并等待 NORMAL
  - cvm_launcher.py：RunInstances 启动实例并等待 RUNNING
  - benchmark.py：SSH 执行 scripts/benchmark.sh 采集结果
  - reporter.py：渲染 templates/report.html.j2 输出报告
- scripts/benchmark.sh：基准测试脚本（最后一行输出 JSON）
- reports/：报告输出目录（report_latest.html / report_*.html / report_*.json）
- logs/：运行日志（pipeline.log 等）
- state.db：默认 SQLite 状态库文件（可在 .env 修改）

## 运行环境要求

- Python：3.10+（代码使用了 `X | Y` 类型语法）
- 操作系统：
  - Windows：支持运行全流程；镜像注入阶段建议启用 WSL（自动注入 cloud-init）
  - Linux：可运行；如要“直接注入镜像内部文件”，需要 guestfish 或 qemu-nbd 等工具
- 腾讯云侧准备：
  - COS：一个 Bucket（建议私有读）
  - CVM：具备导入镜像、创建实例、销毁实例等权限
  - 网络：允许创建实例并分配公网 IP；安全组需开放 22 端口供 SSH benchmark

## 快速开始

### 1) 安装依赖

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

运行测试（可选）：

```bash
pip install pytest
pytest -q
```

### 2) 配置 .env（项目根目录）

项目会自动加载根目录下的 `.env`。最小可运行配置如下（示例值请替换）：

```dotenv
# ---- 腾讯云基础凭证 ----
TENCENT_SECRET_ID=xxxxxxxxxxxxxxxxxxxx
TENCENT_SECRET_KEY=xxxxxxxxxxxxxxxxxxxx
TENCENT_REGION=ap-guangzhou
TENCENT_COS_BUCKET=example-1234567890

# ---- 阿里云镜像检测（可选）----
ALI_IMAGE_DOC_URL=https://mirrors.aliyun.com/alinux/
CHECK_INTERVAL_HOURS=6

# ---- CVM 实例参数（建议明确填写）----
CVM_INSTANCE_TYPE=S5.MEDIUM2
CVM_VPC_ID=
CVM_SUBNET_ID=
CVM_SECURITY_GROUP_ID=
CVM_LOGIN_PASSWORD=Ali2Tencent@2024
CVM_DISK_SIZE=50

# ---- SSH / 性能测试 ----
SSH_USER=root
SSH_PRIVATE_KEY_PATH=
BENCHMARK_TIMEOUT=300

# ---- 运行行为 ----
KEEP_INSTANCE=false
TMP_DIR=
LOG_DIR=
REPORT_DIR=
STATE_DB=
```

说明：

- `TENCENT_SECRET_ID / TENCENT_SECRET_KEY` 属于敏感信息：只放本地 `.env`，不要提交到仓库。
- `CVM_VPC_ID / CVM_SUBNET_ID / CVM_SECURITY_GROUP_ID` 为空时，CVM 会按默认逻辑创建（可能在某些账号/地域受限）；建议明确填好，尤其是安全组要放通 SSH。
- `SSH_PRIVATE_KEY_PATH` 若填写，会优先用密钥登录 benchmark；为空则用 `CVM_LOGIN_PASSWORD`。

## 使用方式（命令行）

查看帮助：

```bash
python main.py --help
```

### 执行一次流水线

- 自动检测新版本并跑完整流程：

```bash
python main.py run
```

- 指定版本号强制执行（不依赖“是否新版本”）：

```bash
python main.py run --version 3.2
```

- 从指定阶段断点续跑（常用于失败恢复）：

```bash
python main.py run --version 3.2 --stage upload
```

- 跑到某个阶段就停止（含该阶段）：

```bash
python main.py run --version 3.2 --stop-stage import
```

### 守护模式（定时检测新版本）

```bash
python main.py daemon
```

### 查看任务状态

```bash
python main.py status
python main.py status --version 3.2
```

### 生成报告（基于 state.db 的历史结果）

```bash
python main.py report
```

默认会在 `reports/` 生成：

- report_latest.html（每次覆盖）
- report_YYYYMMDD_HHMMSS.html
- report_YYYYMMDD_HHMMSS.json

## 流水线阶段说明

流水线阶段固定顺序如下：

1. monitor：解析阿里云页面/目录，找出镜像版本与下载 URL，并写入 state.db
2. download：下载镜像到 `TMP_DIR`（默认 tmp/），支持断点续传，并计算 SHA256
3. modify：为腾讯云准备 cloud-init 配置，尽可能将配置注入镜像；无法注入时会生成参考文件（user-data.txt / meta-data.txt / 99_qcloud.cfg / cloud.cfg）
4. upload：上传到 COS（images/<version>/...），生成 24h 预签名 URL（ImportImage 使用）
5. import：调用 ImportImage，轮询 DescribeImages 等待镜像状态 NORMAL
6. launch：RunInstances 创建按量实例，轮询 DescribeInstances 等待 RUNNING 并拿到公网 IP
7. benchmark：SSH 上去执行 scripts/benchmark.sh，解析最后一行 JSON 写入 benchmark_results 表；默认测试后会销毁实例（KEEP_INSTANCE=false）
8. report：渲染 HTML/JSON 报告

可用阶段名（与 `--stage`/`--stop-stage` 对应）：

`monitor/download/modify/upload/import/launch/benchmark/report`

## 数据与产物

- SQLite（默认 state.db）
  - image_versions：镜像版本发现记录（processed=1 表示全流程完成）
  - pipeline_tasks：每次执行的阶段状态（running/done/failed）与 meta（JSON）
  - benchmark_results：基准测试结果（含 raw_json 完整输出）
- 日志：logs/pipeline.log（按天滚动，保留 30 天）
- 临时文件：tmp/（下载的镜像、修改后的镜像、cloud-init 参考文件）
- 报告：reports/report_latest.html + 带时间戳的历史文件

## 常见问题（排障指北）

### 1) modify 阶段提示“未找到任何可用的镜像修改工具”

这表示没法自动把 cloud-init 配置写进镜像内部。此时：

- 程序仍会复制出 `<name>_modified.qcow2` 继续后续流程
- 同时会在 tmp/ 目录保存参考文件（99_qcloud.cfg / cloud.cfg 等）

Windows 推荐方案：

- 安装并启用 WSL（Ubuntu 等）
- 在 WSL 内安装 qemu-utils：`sudo apt-get install -y qemu-utils`
- 重新跑 `--stage modify` 或从更早阶段重跑

### 2) benchmark 阶段 SSH 连接失败

优先检查：

- 安全组是否放通 22
- `SSH_USER` 是否正确（不同发行版默认用户不同）
- 若使用密钥登录：`SSH_PRIVATE_KEY_PATH` 是否存在且与实例匹配
- 若使用密码登录：`CVM_LOGIN_PASSWORD` 是否生效（镜像 cloud-init/sshd 配置是否允许密码）

### 3) 网络测试结果 net_bandwidth_mb 为 0

当前 scripts/benchmark.sh 里的网络测试默认返回 0（未配置 iperf3 服务端）。如果你希望测带宽，需要自建 iperf3 server 并改造脚本的 net_test()。

### 4) ImportImage 等待时间较长/失败

- 镜像导入本身会比较慢，cvm_importer 默认最长等待 2 小时
- ImportImage 使用 COS 预签名下载 URL（有效期 24h）；若你修改了流程导致导入延迟很久，可能需要重新触发 upload/import 阶段生成新 URL

## 安全提醒

- 不要把 `.env`（尤其是 SecretId/SecretKey）提交到仓库
- `state.db` / logs/ / reports/ 可能包含资源 ID、IP、错误栈等敏感信息，按你的合规要求处理
