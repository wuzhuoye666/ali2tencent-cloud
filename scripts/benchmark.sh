#!/bin/bash
# ali2tencent benchmark script
# 依赖：sysbench, fio, iperf3（自动安装）
# 输出：最后一行为 JSON 格式的测试结果

set -euo pipefail

# ---- 安装依赖 ----
install_deps() {
    if command -v yum &>/dev/null; then
        yum install -y -q sysbench fio iperf3 2>/dev/null || true
    elif command -v apt-get &>/dev/null; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y -q sysbench fio iperf3 2>/dev/null || true
    fi
}

install_deps

# ---- CPU 测试 ----
cpu_test() {
    if command -v sysbench &>/dev/null; then
        result=$(sysbench cpu --cpu-max-prime=10000 --threads=4 --time=30 run 2>/dev/null)
        echo "$result" | grep "events per second" | awk '{print $NF}'
    else
        echo "0"
    fi
}

# ---- 内存测试 ----
mem_test() {
    if command -v sysbench &>/dev/null; then
        result=$(sysbench memory --memory-block-size=1M --memory-total-size=10G --threads=4 run 2>/dev/null)
        echo "$result" | grep "MiB transferred" | grep -oP '[\d.]+(?= MiB/sec)' || echo "0"
    else
        echo "0"
    fi
}

# ---- 磁盘 I/O 测试 ----
disk_test() {
    local TESTFILE="/tmp/ali2tencent_fio_test"
    if command -v fio &>/dev/null; then
        # 顺序读
        read_bw=$(fio --name=seq_read --ioengine=libaio --rw=read --bs=1M \
            --size=512M --numjobs=1 --iodepth=8 --runtime=30 --time_based \
            --filename="$TESTFILE" --output-format=json 2>/dev/null \
            | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['jobs'][0]['read']['bw']/1024)" 2>/dev/null || echo "0")
        # 顺序写
        write_bw=$(fio --name=seq_write --ioengine=libaio --rw=write --bs=1M \
            --size=512M --numjobs=1 --iodepth=8 --runtime=30 --time_based \
            --filename="$TESTFILE" --output-format=json 2>/dev/null \
            | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['jobs'][0]['write']['bw']/1024)" 2>/dev/null || echo "0")
        rm -f "$TESTFILE"
        echo "${read_bw}:${write_bw}"
    else
        echo "0:0"
    fi
}

# ---- 网络测试（可选，需要 iperf3 服务端） ----
# 如果没有 iperf3 服务端，直接返回 0
net_test() {
    echo "0"
}

# ---- 系统信息 ----
get_sysinfo() {
    os_release=$(cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d'"' -f2 || echo "unknown")
    kernel=$(uname -r)
    cpu_model=$(grep "model name" /proc/cpuinfo | head -1 | cut -d':' -f2 | xargs || echo "unknown")
    mem_total=$(grep MemTotal /proc/meminfo | awk '{print $2}')
    disk_total=$(df -BG / | tail -1 | awk '{print $2}' | tr -d G)
    echo "${os_release}|${kernel}|${cpu_model}|${mem_total}|${disk_total}"
}

# ---- 主测试流程 ----
echo "=== ali2tencent benchmark start ==="

echo ">> CPU test..."
CPU_SCORE=$(cpu_test)

echo ">> Memory test..."
MEM_SCORE=$(mem_test)

echo ">> Disk I/O test..."
DISK_RESULT=$(disk_test)
DISK_READ=$(echo "$DISK_RESULT" | cut -d':' -f1)
DISK_WRITE=$(echo "$DISK_RESULT" | cut -d':' -f2)

echo ">> Network test..."
NET_BW=$(net_test)

echo ">> System info..."
SYS_INFO=$(get_sysinfo)
OS_RELEASE=$(echo "$SYS_INFO" | cut -d'|' -f1)
KERNEL=$(echo "$SYS_INFO" | cut -d'|' -f2)
CPU_MODEL=$(echo "$SYS_INFO" | cut -d'|' -f3)
MEM_TOTAL=$(echo "$SYS_INFO" | cut -d'|' -f4)
DISK_TOTAL=$(echo "$SYS_INFO" | cut -d'|' -f5)

echo "=== benchmark done ==="

# 输出 JSON 结果（最后一行）
python3 - <<PYEOF
import json, datetime
result = {
    "tested_at": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
    "cpu_score": float("${CPU_SCORE}" or 0),
    "mem_score": float("${MEM_SCORE}" or 0),
    "disk_read_mb": float("${DISK_READ}" or 0),
    "disk_write_mb": float("${DISK_WRITE}" or 0),
    "net_bandwidth_mb": float("${NET_BW}" or 0),
    "os_release": "${OS_RELEASE}",
    "kernel": "${KERNEL}",
    "cpu_model": "${CPU_MODEL}",
    "mem_total_kb": int("${MEM_TOTAL}" or 0),
    "disk_total_gb": int("${DISK_TOTAL}" or 0),
}
print(json.dumps(result))
PYEOF
