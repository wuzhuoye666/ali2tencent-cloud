---
name: ali2tencent
description: 将阿里云镜像迁移到腾讯云（monitor -> download -> modify -> upload -> import）
---

# 阿里云镜像迁移到腾讯云

将阿里云 Alibaba Cloud Linux 镜像自动迁移到腾讯云 CVM。

## 前置要求

在项目根目录 `.env` 文件中配置以下内容：

```dotenv
TENCENT_SECRET_ID=your_secret_id
TENCENT_SECRET_KEY=your_secret_key
TENCENT_REGION=ap-guangzhou
TENCENT_COS_BUCKET=your-bucket-123456
```

## 执行流程

流水线包含 5 个阶段：

1. **monitor** - 检测阿里云新版本镜像
2. **download** - 下载镜像（支持断点续传）
3. **modify** - 注入腾讯云 cloud-init 配置
4. **upload** - 上传到 COS
5. **import** - 导入为腾讯云自定义镜像

## 使用方法

```bash
# 执行完整流程
python main.py run

# 指定版本
python main.py run --version 3

# 从指定阶段恢复
python main.py run --version 3 --stage upload

# 查看任务状态
python main.py status
```

## 注意事项

- 需要 guestfish 或 qemu-nbd 用于镜像注入
- 导入的镜像 OsVersion 设置为 8（对应 Alinux 3）
- state.db 不会提交到 git（包含敏感信息）
