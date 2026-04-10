#!/bin/bash
# PCILeech Donor Device Extractor
# 在装有目标真实设备的 Linux 机器上以 root 运行
# 用法: sudo ./extract_donor.sh <BDF>
# 示例: sudo ./extract_donor.sh 03:00.0

set -euo pipefail

BDF="${1:-}"
if [ -z "$BDF" ]; then
    echo "用法: sudo $0 <PCI BDF>"
    echo ""
    echo "可用的 PCIe 设备:"
    lspci -nn | grep -iE "net|wifi|wireless|ethernet" || lspci -nn
    exit 1
fi

# 验证 BDF 存在
if ! lspci -s "$BDF" > /dev/null 2>&1; then
    echo "错误: 设备 $BDF 不存在"
    exit 1
fi

DEVICE_DESC=$(lspci -s "$BDF" -nn)
echo "=========================================="
echo " PCILeech Donor Device Extractor"
echo "=========================================="
echo "目标设备: $DEVICE_DESC"
echo ""

OUT_DIR="donor_$(echo "$BDF" | tr ':.' '_')"
mkdir -p "$OUT_DIR"

# --- 1. 基本设备信息 ---
echo "[1/5] 提取基本设备信息..."
lspci -s "$BDF" -vvv > "$OUT_DIR/lspci_verbose.txt" 2>&1
lspci -s "$BDF" -nn  > "$OUT_DIR/lspci_ids.txt" 2>&1

# 解析 VID/DID/SubVID/SubDID
VID=$(setpci -s "$BDF" 0x00.W 2>/dev/null || echo "0000")
DID=$(setpci -s "$BDF" 0x02.W 2>/dev/null || echo "0000")
REV=$(setpci -s "$BDF" 0x08.B 2>/dev/null || echo "00")
CLASS=$(setpci -s "$BDF" 0x09.B 2>/dev/null || echo "00")$(setpci -s "$BDF" 0x0a.B 2>/dev/null || echo "00")$(setpci -s "$BDF" 0x0b.B 2>/dev/null || echo "00")
SUB_VID=$(setpci -s "$BDF" 0x2c.W 2>/dev/null || echo "0000")
SUB_DID=$(setpci -s "$BDF" 0x2e.W 2>/dev/null || echo "0000")

echo "  Vendor ID:            0x$VID"
echo "  Device ID:            0x$DID"
echo "  Revision ID:          0x$REV"
echo "  Class Code:           0x$CLASS"
echo "  Subsystem Vendor ID:  0x$SUB_VID"
echo "  Subsystem Device ID:  0x$SUB_DID"

# --- 2. 完整 4KB 配置空间 ---
echo "[2/5] 提取 4KB PCIe 配置空间..."

CONFIG_SYS="/sys/bus/pci/devices/0000:$BDF/config"
if [ -f "$CONFIG_SYS" ]; then
    # 直接从 sysfs 读取完整 4KB
    cp "$CONFIG_SYS" "$OUT_DIR/config_raw.bin"
    CFGSIZE=$(stat -c%s "$OUT_DIR/config_raw.bin" 2>/dev/null || stat -f%z "$OUT_DIR/config_raw.bin" 2>/dev/null)
    echo "  已提取 $CFGSIZE 字节配置空间（二进制）"
else
    # 回退到 lspci -xxx（仅 256 字节标准空间）
    echo "  警告: 无法访问 sysfs，回退到 lspci（仅 256 字节）"
    lspci -s "$BDF" -xxx > "$OUT_DIR/config_lspci.txt" 2>&1
fi

# 同时保存文本格式方便查看
lspci -s "$BDF" -xxxx > "$OUT_DIR/config_hex.txt" 2>&1 || \
lspci -s "$BDF" -xxx  > "$OUT_DIR/config_hex.txt" 2>&1 || true

# --- 3. 提取 DSN (Device Serial Number) ---
echo "[3/5] 提取设备序列号 (DSN)..."

DSN="0000000000000000"
# 从 lspci 输出中查找 DSN Capability
DSN_LINE=$(lspci -s "$BDF" -vvv 2>/dev/null | grep -i "device serial number" || true)
if [ -n "$DSN_LINE" ]; then
    # 格式通常是: Device Serial Number xx-xx-xx-xx-xx-xx-xx-xx
    DSN_RAW=$(echo "$DSN_LINE" | grep -oE '[0-9a-fA-F]{2}(-[0-9a-fA-F]{2}){7}' || true)
    if [ -n "$DSN_RAW" ]; then
        # 去掉横线，转为 64-bit hex
        DSN=$(echo "$DSN_RAW" | tr -d '-')
        echo "  DSN: 0x$DSN (raw: $DSN_RAW)"
    else
        echo "  DSN 格式解析失败，原始行: $DSN_LINE"
    fi
else
    echo "  未找到 DSN Capability（设备可能不支持）"
fi

# --- 4. BAR 信息 ---
echo "[4/5] 提取 BAR 信息..."
lspci -s "$BDF" -vvv 2>/dev/null | grep -E "Region [0-9]|Memory at|I/O ports" > "$OUT_DIR/bar_info.txt" || true
cat "$OUT_DIR/bar_info.txt" 2>/dev/null | head -6 | sed 's/^/  /'

# --- 5. 生成汇总文件 ---
echo "[5/5] 生成汇总..."

cat > "$OUT_DIR/donor_summary.txt" << EOF
# PCILeech Donor Device Summary
# 提取时间: $(date -u +"%Y-%m-%d %H:%M:%S UTC")
# 设备 BDF: $BDF
# 设备描述: $DEVICE_DESC

VENDOR_ID=0x$VID
DEVICE_ID=0x$DID
REVISION_ID=0x$REV
CLASS_CODE=0x$CLASS
SUBSYSTEM_VENDOR_ID=0x$SUB_VID
SUBSYSTEM_DEVICE_ID=0x$SUB_DID
DSN=0x$DSN
EOF

echo ""
echo "=========================================="
echo " 提取完成！"
echo "=========================================="
echo "输出目录: $OUT_DIR/"
echo ""
echo "文件列表:"
ls -la "$OUT_DIR/"
echo ""
echo "下一步: 将 $OUT_DIR/ 目录拷贝回你的开发机"
echo "然后运行: python3 tools/generate_coe.py $OUT_DIR/"
