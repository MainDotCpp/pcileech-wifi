#!/usr/bin/env python3
"""
PCILeech 一键固件生成工具
从 RWEverything 导出的 PCIe 配置空间二进制文件，自动完成代码修改并调用 Vivado 构建固件。

用法:
  python3 generate_coe.py <config_raw.bin> [--build] [--board <board>]

参数:
  --build          修改完成后自动调用 Vivado 构建固件
  --board <board>  指定板卡 (默认: captain_75T)
                   可选: squirrel, m2, enigma_x1, immortal_75T, immortal_75Ts, captain_75T, 100t

示例:
  python3 generate_coe.py P030000.bin                        # 仅修改源码
  python3 generate_coe.py P030000.bin --build                # 修改 + 构建
  python3 generate_coe.py P030000.bin --build --board m2     # 指定板卡

RWEverything 导出方法:
  1. 打开 RWEverything → PCI 选项卡
  2. 选择目标网卡
  3. Access → PCIE (4096 bytes)
  4. File → Save → 保存为 .bin 文件
"""

import sys
import os
import re
import struct
import subprocess
import shutil
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 板卡配置
# ---------------------------------------------------------------------------

BOARDS = {
    'squirrel': {
        'generate_tcl': 'vivado_generate_project_squirrel.tcl',
        'build_tcl': 'vivado_build.tcl',
        'project_name': 'pcileech_squirrel',
        'top_module': 'pcileech_squirrel_top',
        'suffix': '35t',
    },
    'm2': {
        'generate_tcl': 'vivado_generate_project_m2.tcl',
        'build_tcl': 'vivado_build.tcl',
        'project_name': 'pcileech_screamer_m2',
        'top_module': 'pcileech_squirrel_top',
        'suffix': '35t',
    },
    'enigma_x1': {
        'generate_tcl': 'vivado_generate_project_enigma_x1.tcl',
        'build_tcl': 'vivado_build.tcl',
        'project_name': 'pcileech_enigma_x1',
        'top_module': 'pcileech_enigma_x1_top',
        'suffix': '75t',
    },
    'immortal_75T': {
        'generate_tcl': 'vivado_generate_project_immortal_75T.tcl',
        'build_tcl': 'vivado_build.tcl',
        'project_name': 'pcileech_enigma_x1',
        'top_module': 'pcileech_enigma_x1_top',
        'suffix': '75t',
    },
    'immortal_75Ts': {
        'generate_tcl': 'vivado_generate_project_immortal_75Ts.tcl',
        'build_tcl': 'vivado_build.tcl',
        'project_name': 'pcileech_squirrel',
        'top_module': 'pcileech_squirrel_top',
        'suffix': '75ts',
    },
    'captain_75T': {
        'generate_tcl': 'vivado_generate_project_captain_75T.tcl',
        'build_tcl': 'vivado_build.tcl',
        'project_name': 'pcileech_enigma_x1',
        'top_module': 'pcileech_enigma_x1_top',
        'suffix': '75t',
    },
    '100t': {
        'generate_tcl': 'vivado_generate_project_100t.tcl',
        'build_tcl': 'vivado_build_100t.tcl',
        'project_name': 'pcileech_tbx4_100t',
        'top_module': 'pcileech_tbx4_100t_top',
        'suffix': '100t',
    },
}


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def read_config_binary(path: str) -> bytes:
    """读取二进制配置空间文件"""
    with open(path, 'rb') as f:
        data = f.read()
    if len(data) < 256:
        print(f"  警告: 文件仅 {len(data)} 字节，标准 PCIe 配置空间至少 256 字节")
    if len(data) < 4096:
        print(f"  提示: 文件 {len(data)} 字节，不足 4096 将补零（扩展能力区域将为空）")
        data += b'\x00' * (4096 - len(data))
    return data[:4096]


def find_dsn_in_config(data: bytes) -> str:
    """从 4KB 扩展配置空间中自动解析 DSN (Cap ID = 0x0003)"""
    offset = 0x100
    while 0 < offset < 0xFFC:
        cap_header = struct.unpack_from('<I', data, offset)[0]
        cap_id = cap_header & 0xFFFF
        next_offset = (cap_header >> 20) & 0xFFC

        if cap_id == 0x0003:
            dsn_lo = struct.unpack_from('<I', data, offset + 4)[0]
            dsn_hi = struct.unpack_from('<I', data, offset + 8)[0]
            dsn = (dsn_hi << 32) | dsn_lo
            if dsn != 0:
                return f"{dsn:016X}"
            return "0000000000000000"

        if next_offset == 0 or next_offset <= offset:
            break
        offset = next_offset

    return "0000000000000000"


def parse_device_info(data: bytes) -> dict:
    """从配置空间二进制解析所有设备信息"""
    vid = struct.unpack_from('<H', data, 0x00)[0]
    did = struct.unpack_from('<H', data, 0x02)[0]
    rev = data[0x08]
    class_base = data[0x0b]
    class_sub = data[0x0a]
    class_intf = data[0x09]
    class_code = (class_base << 16) | (class_sub << 8) | class_intf
    sub_vid = struct.unpack_from('<H', data, 0x2c)[0]
    sub_did = struct.unpack_from('<H', data, 0x2e)[0]
    dsn = find_dsn_in_config(data)

    return {
        'vid': vid,
        'did': did,
        'rev': rev,
        'class_code': class_code,
        'class_base': class_base,
        'class_sub': class_sub,
        'class_intf': class_intf,
        'sub_vid': sub_vid,
        'sub_did': sub_did,
        'dsn': dsn,
    }


# ---------------------------------------------------------------------------
# COE 生成
# ---------------------------------------------------------------------------

def config_to_dwords(data: bytes) -> list[int]:
    """将字节数据转换为 32-bit DWORD 列表（小端）"""
    return [struct.unpack_from('<I', data, i)[0] for i in range(0, len(data), 4)]


def generate_coe(dwords: list[int], path: str) -> None:
    """生成 Vivado .coe 文件"""
    with open(path, 'w') as f:
        f.write("memory_initialization_radix=16;\n")
        f.write("memory_initialization_vector=\n\n")
        for i in range(0, len(dwords), 4):
            chunk = dwords[i:i + 4]
            line = ','.join(f'{dw:08x}' for dw in chunk)
            line += ';' if i + 4 >= len(dwords) else ','
            f.write(line + '\n')


# ---------------------------------------------------------------------------
# 源码修改
# ---------------------------------------------------------------------------

def patch_file(path: str, old: str, new: str) -> bool:
    """替换文件中的精确文本，返回是否成功"""
    with open(path, 'r') as f:
        content = f.read()
    if old not in content:
        return False
    content = content.replace(old, new, 1)
    with open(path, 'w') as f:
        f.write(content)
    return True


def patch_xci(path: str, info: dict) -> int:
    """修改 pcie_7x_0.xci 中的设备身份参数，返回修改次数"""
    with open(path, 'r') as f:
        content = f.read()

    replacements = {
        (r'("Vendor_ID"\s*:\s*\[\s*\{\s*"value"\s*:\s*)"[0-9A-Fa-f]+"',
         f'\\1"{info["vid"]:04X}"'),
        (r'("Device_ID"\s*:\s*\[\s*\{\s*"value"\s*:\s*)"[0-9A-Fa-f]+"',
         f'\\1"{info["did"]:04X}"'),
        (r'("Revision_ID"\s*:\s*\[\s*\{\s*"value"\s*:\s*)"[0-9A-Fa-f]+"',
         f'\\1"{info["rev"]:02X}"'),
        (r'("Subsystem_Vendor_ID"\s*:\s*\[\s*\{\s*"value"\s*:\s*)"[0-9A-Fa-f]+"',
         f'\\1"{info["sub_vid"]:04X}"'),
        (r'("Subsystem_ID"\s*:\s*\[\s*\{\s*"value"\s*:\s*)"[0-9A-Fa-f]+"',
         f'\\1"{info["sub_did"]:04X}"'),
        (r'("Class_Code_Base"\s*:\s*\[\s*\{\s*"value"\s*:\s*)"[0-9A-Fa-f]+"',
         f'\\1"{info["class_base"]:02X}"'),
        (r'("Class_Code_Sub"\s*:\s*\[\s*\{\s*"value"\s*:\s*)"[0-9A-Fa-f]+"',
         f'\\1"{info["class_sub"]:02X}"'),
        (r'("Class_Code_Interface"\s*:\s*\[\s*\{\s*"value"\s*:\s*)"[0-9A-Fa-f]+"',
         f'\\1"{info["class_intf"]:02X}"'),
        (r'("ven_id"\s*:\s*\[\s*\{\s*"value"\s*:\s*)"[0-9A-Fa-f]+"',
         f'\\1"{info["vid"]:04X}"'),
        (r'("dev_id"\s*:\s*\[\s*\{\s*"value"\s*:\s*)"[0-9A-Fa-f]+"',
         f'\\1"{info["did"]:04X}"'),
        (r'("rev_id"\s*:\s*\[\s*\{\s*"value"\s*:\s*)"[0-9A-Fa-f]+"',
         f'\\1"{info["rev"]:02X}"'),
        (r'("subsys_ven_id"\s*:\s*\[\s*\{\s*"value"\s*:\s*)"[0-9A-Fa-f]+"',
         f'\\1"{info["sub_vid"]:04X}"'),
        (r'("subsys_id"\s*:\s*\[\s*\{\s*"value"\s*:\s*)"[0-9A-Fa-f]+"',
         f'\\1"{info["sub_did"]:04X}"'),
        (r'("class_code"\s*:\s*\[\s*\{\s*"value"\s*:\s*)"[0-9A-Fa-f]+"',
         f'\\1"{info["class_code"]:06X}"'),
    }

    count = 0
    for pattern, repl in replacements:
        content, n = re.subn(pattern, repl, content)
        count += n

    with open(path, 'w') as f:
        f.write(content)
    return count


def patch_fifo_sv(path: str, info: dict) -> list[str]:
    """修改 pcileech_fifo.sv 中的设备身份和控制位"""
    results = []

    ok = patch_file(path,
        "reg     [79:0]      _pcie_core_config = { 4'hf, 1'b1, 1'b1, 1'b0, 1'b0, 8'h02, 16'h0666, 16'h10EE, 16'h0007, 16'h10EE };",
        f"reg     [79:0]      _pcie_core_config = {{ 4'hf, 1'b1, 1'b1, 1'b0, 1'b0, 8'h{info['rev']:02x}, 16'h{info['did']:04x}, 16'h{info['vid']:04x}, 16'h{info['sub_did']:04x}, 16'h{info['sub_vid']:04x} }};")
    results.append(f"  _pcie_core_config: {'OK' if ok else 'SKIP (已修改或格式不匹配)'}")

    ok = patch_file(path,
        "rw[143:128] <= 16'h10EE;                    // +010: CFG_SUBSYS_VEND_ID (NOT IMPLEMENTED)",
        f"rw[143:128] <= 16'h{info['sub_vid']:04X};                    // +010: CFG_SUBSYS_VEND_ID (NOT IMPLEMENTED)")
    results.append(f"  CFG_SUBSYS_VEND_ID: {'OK' if ok else 'SKIP'}")

    ok = patch_file(path,
        "rw[159:144] <= 16'h0007;                    // +012: CFG_SUBSYS_ID      (NOT IMPLEMENTED)",
        f"rw[159:144] <= 16'h{info['sub_did']:04X};                    // +012: CFG_SUBSYS_ID      (NOT IMPLEMENTED)")
    results.append(f"  CFG_SUBSYS_ID:      {'OK' if ok else 'SKIP'}")

    ok = patch_file(path,
        "rw[175:160] <= 16'h10EE;                    // +014: CFG_VEND_ID        (NOT IMPLEMENTED)",
        f"rw[175:160] <= 16'h{info['vid']:04X};                    // +014: CFG_VEND_ID        (NOT IMPLEMENTED)")
    results.append(f"  CFG_VEND_ID:        {'OK' if ok else 'SKIP'}")

    ok = patch_file(path,
        "rw[191:176] <= 16'h0666;                    // +016: CFG_DEV_ID         (NOT IMPLEMENTED)",
        f"rw[191:176] <= 16'h{info['did']:04X};                    // +016: CFG_DEV_ID         (NOT IMPLEMENTED)")
    results.append(f"  CFG_DEV_ID:         {'OK' if ok else 'SKIP'}")

    ok = patch_file(path,
        "rw[199:192] <= 8'h02;                       // +018: CFG_REV_ID         (NOT IMPLEMENTED)",
        f"rw[199:192] <= 8'h{info['rev']:02X};                       // +018: CFG_REV_ID         (NOT IMPLEMENTED)")
    results.append(f"  CFG_REV_ID:         {'OK' if ok else 'SKIP'}")

    ok = patch_file(path,
        "rw[203]     <= 1'b1;                        //       CFGTLP ZERO DATA",
        "rw[203]     <= 1'b0;                        //       CFGTLP ZERO DATA (CUSTOM CFG ENABLED)")
    results.append(f"  CFGTLP_ZERO→0:     {'OK' if ok else 'SKIP (已修改)'}")

    return results


def patch_cfg_sv(path: str, info: dict) -> list[str]:
    """修改 pcileech_pcie_cfg_a7.sv 中的 DSN"""
    results = []

    ok = patch_file(path,
        "rw[127:64]  <= 64'h0000000000000000;    // +008: cfg_dsn",
        f"rw[127:64]  <= 64'h{info['dsn']};    // +008: cfg_dsn")
    results.append(f"  DSN: {'OK' if ok else 'SKIP (已修改或 DSN 为零)'}")

    return results


# ---------------------------------------------------------------------------
# Git 重置 — 支持重复运行
# ---------------------------------------------------------------------------

TRACKED_FILES = [
    'ip/pcileech_cfgspace.coe',
    'ip/100t/pcileech_cfgspace.coe',
    'ip/pcie_7x_0.xci',
    'ip/100t/pcie_7x_0.xci',
    'src/pcileech_fifo.sv',
    'src/pcileech_pcie_cfg_a7.sv',
]


def git_reset_sources(project_root: Path) -> None:
    """将需要修改的文件还原到 git HEAD 状态，确保 patch 匹配原始文本"""
    print("\n[0/6] 还原源码到 git HEAD（确保可重复运行）...")
    for rel in TRACKED_FILES:
        full = project_root / rel
        if not full.exists():
            continue
        result = subprocess.run(
            ['git', 'checkout', 'HEAD', '--', rel],
            cwd=str(project_root),
            capture_output=True, text=True,
        )
        status = 'OK' if result.returncode == 0 else f'FAIL: {result.stderr.strip()}'
        print(f"  {rel}: {status}")


# ---------------------------------------------------------------------------
# Vivado 构建
# ---------------------------------------------------------------------------

def find_vivado() -> str | None:
    """查找 vivado 可执行文件"""
    # 1. PATH 中查找
    vivado = shutil.which('vivado')
    if vivado:
        return vivado
    # 2. 常见安装路径
    for year in range(2024, 2019, -1):
        for base in ['/tools/Xilinx/Vivado', '/opt/Xilinx/Vivado']:
            candidate = f'{base}/{year}.1/bin/vivado'
            if os.path.isfile(candidate):
                return candidate
            candidate = f'{base}/{year}.2/bin/vivado'
            if os.path.isfile(candidate):
                return candidate
    return None


def vivado_build(project_root: Path, board_cfg: dict) -> Path | None:
    """调用 Vivado 批处理模式生成固件，返回输出文件路径"""
    vivado = find_vivado()
    if not vivado:
        print("\n  错误: 未找到 vivado，请确认已安装 Vivado 并将其加入 PATH")
        print("  提示: source /tools/Xilinx/Vivado/<版本>/settings64.sh")
        return None

    print(f"  Vivado: {vivado}")

    proj_name = board_cfg['project_name']
    top_module = board_cfg['top_module']
    gen_tcl = board_cfg['generate_tcl']
    build_tcl = board_cfg['build_tcl']

    # 清理旧的 Vivado 工程目录
    proj_dir = project_root / proj_name
    if proj_dir.exists():
        print(f"  清理旧工程目录: {proj_name}/")
        shutil.rmtree(proj_dir)

    # 步骤 1: 生成工程
    print(f"\n  [BUILD 1/2] 生成 Vivado 工程 ({gen_tcl})...")
    result = subprocess.run(
        [vivado, '-mode', 'batch', '-source', gen_tcl, '-notrace'],
        cwd=str(project_root),
        timeout=600,
    )
    if result.returncode != 0:
        print(f"  错误: 工程生成失败 (退出码 {result.returncode})")
        return None

    # 步骤 2: 综合 + 实现 + 比特流
    print(f"\n  [BUILD 2/2] 综合/实现/比特流生成 ({build_tcl})...")
    print("  这将需要较长时间（通常 30-60 分钟）...")

    # 重写 build tcl 以使用正确的项目名和顶层模块
    build_tcl_content = f"""
open_project ./{proj_name}/{proj_name}.xpr
puts "-------------------------------------------------------"
puts " STARTING SYNTHESIS STEP.                              "
puts "-------------------------------------------------------"
launch_runs synth_1
wait_on_run synth_1
puts "-------------------------------------------------------"
puts " STARTING IMPLEMENTATION STEP.                         "
puts "-------------------------------------------------------"
launch_runs impl_1 -to_step write_bitstream
wait_on_run impl_1
puts "-------------------------------------------------------"
puts " BUILD COMPLETED.                                      "
puts "-------------------------------------------------------"
"""
    tmp_build_tcl = project_root / '_build_tmp.tcl'
    tmp_build_tcl.write_text(build_tcl_content)

    result = subprocess.run(
        [vivado, '-mode', 'batch', '-source', str(tmp_build_tcl), '-notrace'],
        cwd=str(project_root),
        timeout=7200,  # 2 小时超时
    )
    tmp_build_tcl.unlink(missing_ok=True)

    if result.returncode != 0:
        print(f"  错误: 构建失败 (退出码 {result.returncode})")
        return None

    # 查找生成的 .bin 文件
    bin_file = project_root / proj_name / f'{proj_name}.runs' / 'impl_1' / f'{top_module}.bin'
    if not bin_file.exists():
        print(f"  错误: 未找到输出文件 {bin_file}")
        # 尝试搜索
        for f in (project_root / proj_name).rglob('*.bin'):
            print(f"  发现: {f}")
        return None

    return bin_file


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> tuple[str, bool, str]:
    """解析命令行参数"""
    bin_path = ''
    do_build = False
    board = 'captain_75T'

    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == '--build':
            do_build = True
        elif arg == '--board':
            i += 1
            if i >= len(argv):
                print("错误: --board 需要指定板卡名称")
                sys.exit(1)
            board = argv[i]
            if board not in BOARDS:
                print(f"错误: 未知板卡 '{board}'")
                print(f"可选: {', '.join(BOARDS.keys())}")
                sys.exit(1)
        elif not bin_path:
            bin_path = arg
        else:
            print(f"错误: 未知参数 '{arg}'")
            sys.exit(1)
        i += 1

    if not bin_path:
        print(__doc__)
        sys.exit(1)

    return bin_path, do_build, board


def main():
    bin_path, do_build, board_name = parse_args(sys.argv)

    if not os.path.isfile(bin_path):
        print(f"错误: 文件 {bin_path} 不存在")
        sys.exit(1)

    board_cfg = BOARDS[board_name]

    # 定位项目根目录
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent

    fifo_sv = project_root / 'src' / 'pcileech_fifo.sv'
    cfg_sv = project_root / 'src' / 'pcileech_pcie_cfg_a7.sv'
    if not fifo_sv.exists() or not cfg_sv.exists():
        print("错误: 未找到项目源码，请确认脚本位于 pcileech-wifi/tools/ 下")
        sys.exit(1)

    total_steps = 6 if do_build else 5
    step = 0

    print("=" * 55)
    print(" PCILeech 一键固件生成")
    print(f" 板卡: {board_name}")
    print("=" * 55)

    # ── 0. Git 重置 ──
    git_reset_sources(project_root)
    step += 1

    # ── 1. 读取配置空间 ──
    print(f"\n[{step}/{total_steps}] 读取配置空间: {bin_path}")
    config_data = read_config_binary(bin_path)
    info = parse_device_info(config_data)

    print(f"\n  设备身份:")
    print(f"    Vendor ID:           0x{info['vid']:04X}")
    print(f"    Device ID:           0x{info['did']:04X}")
    print(f"    Revision ID:         0x{info['rev']:02X}")
    print(f"    Class Code:          0x{info['class_code']:06X}")
    print(f"    Subsystem Vendor ID: 0x{info['sub_vid']:04X}")
    print(f"    Subsystem Device ID: 0x{info['sub_did']:04X}")
    if info['dsn'] != "0000000000000000":
        print(f"    DSN:                 0x{info['dsn']}")
    else:
        print(f"    DSN:                 未找到")
    step += 1

    # ── 2. 生成并替换 COE 文件 ──
    print(f"\n[{step}/{total_steps}] 生成并替换 COE 文件...")
    dwords = config_to_dwords(config_data)

    coe_targets = [
        project_root / 'ip' / 'pcileech_cfgspace.coe',
        project_root / 'ip' / '100t' / 'pcileech_cfgspace.coe',
    ]
    for target in coe_targets:
        if target.parent.exists():
            generate_coe(dwords, str(target))
            print(f"  OK: {target.relative_to(project_root)}")
    step += 1

    # ── 3. 修改 PCIe IP 核 .xci ──
    print(f"\n[{step}/{total_steps}] 修改 PCIe IP 核 (pcie_7x_0.xci)...")
    xci_files = [
        project_root / 'ip' / 'pcie_7x_0.xci',
        project_root / 'ip' / '100t' / 'pcie_7x_0.xci',
    ]
    for xci in xci_files:
        if xci.exists():
            count = patch_xci(str(xci), info)
            print(f"  OK: {xci.relative_to(project_root)} ({count} 处替换)")
    step += 1

    # ── 4. 修改 SystemVerilog 源码 ──
    print(f"\n[{step}/{total_steps}] 修改 SystemVerilog 源码...")
    print("  pcileech_fifo.sv:")
    for r in patch_fifo_sv(str(fifo_sv), info):
        print(f"  {r}")

    print("  pcileech_pcie_cfg_a7.sv:")
    for r in patch_cfg_sv(str(cfg_sv), info):
        print(f"  {r}")
    step += 1

    # ── 5. 构建 ──
    if do_build:
        print(f"\n[{step}/{total_steps}] 调用 Vivado 构建固件...")
        bin_file = vivado_build(project_root, board_cfg)

        if bin_file and bin_file.exists():
            # 复制到 output/ 目录
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            suffix = board_cfg['suffix']
            output_dir = project_root / 'output'
            output_dir.mkdir(exist_ok=True)
            output_name = f"{timestamp}_{suffix}.bin"
            output_path = output_dir / output_name
            shutil.copy2(str(bin_file), str(output_path))

            print(f"\n  固件已保存: output/{output_name}")
            print(f"  文件大小: {output_path.stat().st_size:,} 字节")
        else:
            print("\n  构建失败，请检查 Vivado 日志")
            sys.exit(1)
        step += 1

    # ── 汇总 ──
    print()
    print("=" * 55)
    print(f"  Donor:      0x{info['vid']:04X}:0x{info['did']:04X} (Rev 0x{info['rev']:02X})")
    print(f"  DSN:        0x{info['dsn']}")
    print(f"  Class Code: 0x{info['class_code']:06X}")
    print(f"  板卡:       {board_name}")
    if not do_build:
        print()
        print("  源码已修改完成。使用 --build 自动调用 Vivado 构建:")
        print(f"    python3 tools/generate_coe.py {bin_path} --build")
    print("=" * 55)


if __name__ == '__main__':
    main()
