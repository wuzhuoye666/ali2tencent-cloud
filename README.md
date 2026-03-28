# ali2tencent-cloud

阿里云（Alibaba Cloud Linux / ALinux 等）镜像自动迁移到腾讯云（CVM）的流水线工具：自动检测新版本 → 下载镜像 → 注入 cloud-init 配置 → 上传 COS → ImportImage 导入自定义镜像。

## 功能特性

- 自动抓取阿里云镜像发布页，发现新版本并入库（SQLite）
- 支持断点续传的镜像下载 + SHA256 校验
- 自动注入腾讯云 cloud-init 配置（datasource_list 包含 ConfigDrive、TencentCloud）
- COS 分片上传 + 生成 24h 预签名下载 URL（供 ImportImage 使用）
- CVM 镜像导入（轮询等待完成）

## 目录结构

```
├── core/
│   ├── config.py      # 从项目根目录 .env 读取配置
│   ├── state.py       # SQLite 状态库（版本、任务）
│   ├── scheduler.py   # daemon 定时调度
│   └── logger.py      # 日志输出到控制台 + logs/pipeline.log
├── pipeline/
│   ├── pipeline.py    # 流水线编排（5 个阶段）
│   ├── monitor.py     # 检测新版本（解析阿里云页面）
│   ├── downloader.py  # 下载镜像（断点续传、校验）
│   ├── image_modifier.py  # cloud-init 注入
│   ├── cos_uploader.py    # 上传 COS 并生成预签名 URL
│   └── cvm_importer.py    # ImportImage 导入自定义镜像
├── logs/              # 运行日志
├── tmp/               # 镜像临时存放目录
├── state.db           # SQLite 状态库
└── main.py            # 命令行入口
```

## 运行环境要求

- Python：3.10+（代码使用了 `X | Y` 类型语法）
- 操作系统：Linux（需要 guestfish 或 qemu-nbd 用于镜像注入）
- 腾讯云侧准备：
  - COS：一个 Bucket（建议私有读）
  - CVM：具备导入镜像权限

## 快速开始

### 1) 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate  # Linux
pip install -r requirements.txt
```

### 2) 配置 .env（项目根目录）

```dotenv
# ---- 腾讯云基础凭证 ----
TENCENT_SECRET_ID=xxxxxxxxxxxxxxxxxxxx
TENCENT_SECRET_KEY=xxxxxxxxxxxxxxxxxxxx
TENCENT_REGION=ap-guangzhou
TENCENT_COS_BUCKET=example-1234567890

# ---- 阿里云镜像检测（可选）----
ALI_IMAGE_DOC_URL=https://mirrors.aliyun.com/alinux/3/image/
CHECK_INTERVAL_HOURS=6
```

## 使用方式（命令行）

### 执行一次流水线

```bash
# 自动检测新版本并跑完整流程
python main.py run

# 指定版本号强制执行
python main.py run --version 3

# 从指定阶段断点续跑
python main.py run --version 3 --stage upload

# 跑到某个阶段就停止
python main.py run --version 3 --stop-stage import
```

### 守护模式（定时检测新版本）

```bash
python main.py daemon
```

### 查看任务状态

```bash
python main.py status
python main.py status --version 3
```

## 流水线阶段说明

1. **monitor**：解析阿里云页面，找出镜像版本与下载 URL，写入 state.db
2. **download**：下载镜像到 `tmp/`，支持断点续传，计算 SHA256
3. **modify**：注入腾讯云 cloud-init 配置（datasource_list: [ConfigDrive, TencentCloud]）
4. **upload**：上传到 COS，生成 24h 预签名 URL
5. **import**：调用 ImportImage，轮询等待镜像状态 NORMAL

## 常见问题

### modify 阶段提示"cloud-init 配置注入失败"

需要安装 guestfish（libguestfs-tools）：

```bash
# Ubuntu/Debian
sudo apt-get install -y libguestfs-tools

# 确保内核文件可读
sudo chmod 644 /boot/vmlinuz-*
```

### ImportImage 提示"不支持导入 Server 22 版本的镜像"

Alinux 3 对应 CentOS 8，需要在 `cvm_importer.py` 中设置 `OsVersion = "8"`。

## 安全提醒

- 不要把 `.env`（尤其是 SecretId/SecretKey）提交到仓库
- `state.db` 和 `logs/` 可能包含敏感信息，按合规要求处理
