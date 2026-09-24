#!/usr/bin/env python3
"""Parallel firmware flashing through one or more CMSIS-DAP probes.

Usage:
    python flash_all.py path\to\firmware.elf
    python flash_all.py path\to\firmware.bin
    python flash_all.py firmware.elf --serial SERIAL --serial SERIAL
    python flash_all.py firmware.elf --exclude-serial SERIAL --exclude-serial SERIAL
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import shutil
import subprocess
import winreg
from ctypes import wintypes
from pathlib import Path


SWD_SPEED_KHZ = 500
BIN_ADDRESS = "0x08000000"

USB_REGISTRY_PATH = r"SYSTEM\CurrentControlSet\Enum\USB"
DAP42_DEVICE_ID = "VID_1209&PID_DA42"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flash firmware through all connected DAP42 probes in parallel."
    )
    parser.add_argument("firmware", type=Path, help="ELF, HEX, or BIN firmware file")
    parser.add_argument(
        "--serial",
        action="append",
        default=[],
        help="programmer serial number; may be specified more than once",
    )
    parser.add_argument(
        "--exclude-serial",
        action="append",
        default=[],
        metavar="SERIAL",
        help="skip this programmer serial number; may be specified more than once",
    )
    parser.add_argument(
        "--openocd",
        type=Path,
        help="path to openocd.exe (also accepted through the OPENOCD environment variable)",
    )
    parser.add_argument(
        "--scripts",
        type=Path,
        help="OpenOCD scripts directory (also accepted through OPENOCD_SCRIPTS)",
    )
    parser.add_argument(
        "--speed",
        type=int,
        default=SWD_SPEED_KHZ,
        metavar="KHZ",
        help=f"initial SWD clock in kHz (default: {SWD_SPEED_KHZ})",
    )
    parser.add_argument(
        "--safe",
        action="store_true",
        help="enable the one-packet CMSIS-DAP compatibility mode",
    )
    parser.add_argument(
        "--no-run",
        action="store_true",
        help="leave the target halted after programming instead of starting it",
    )
    return parser.parse_args()


def cubeide_install_roots() -> list[Path]:
    """Return likely STM32CubeIDE roots without scanning entire disks."""
    roots: list[Path] = []

    configured = os.environ.get("STM32CUBEIDE_PATH")
    if configured:
        roots.append(Path(configured).expanduser())

    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            roots.extend(
                [
                    Path(base) / "STMicroelectronics",
                    Path(base) / "STM32CubeIDE",
                ]
            )

    # The standalone installer uses C:\\ST by default. Also try the same
    # directory on every mounted drive, which covers custom D:\\ST installs.
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        roots.append(Path(f"{letter}:\\ST"))

    for executable_name in ("stm32cubeidec.exe", "stm32cubeide.exe"):
        executable = shutil.which(executable_name)
        if executable:
            roots.append(Path(executable).resolve().parent)

    return list(dict.fromkeys(path.resolve() for path in roots if path.exists()))


def find_openocd(explicit: Path | None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit.expanduser())

    configured = os.environ.get("OPENOCD")
    if configured:
        candidates.append(Path(configured).expanduser())

    on_path = shutil.which("openocd.exe") or shutil.which("openocd")
    if on_path:
        candidates.append(Path(on_path))

    script_dir = Path(__file__).resolve().parent
    candidates.extend(
        [
            script_dir / "openocd.exe",
            script_dir / "openocd" / "bin" / "openocd.exe",
            script_dir.parent / "openocd" / "bin" / "openocd.exe",
        ]
    )

    for root in cubeide_install_roots():
        candidates.extend(
            root.glob(
                "**/plugins/com.st.stm32cube.ide.mcu.externaltools.openocd.win32_*/tools/bin/openocd.exe"
            )
        )

    valid = [path.resolve() for path in candidates if path.is_file()]
    return max(valid, key=lambda path: path.stat().st_mtime) if valid else None


def find_scripts(explicit: Path | None, openocd: Path) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit.expanduser())

    configured = os.environ.get("OPENOCD_SCRIPTS")
    if configured:
        candidates.append(Path(configured).expanduser())

    # Common layouts for upstream/xPack OpenOCD.
    candidates.extend(
        [
            openocd.parent.parent / "share" / "openocd" / "scripts",
            openocd.parent.parent / "openocd" / "scripts",
            openocd.parent.parent / "scripts",
            openocd.parent / "scripts",
        ]
    )

    # If --openocd points inside CubeIDE, locate the sibling scripts plugin
    # without needing to know where CubeIDE itself was installed.
    cube_plugin = next(
        (parent for parent in openocd.parents if parent.parent.name == "plugins"),
        None,
    )
    if cube_plugin is not None:
        candidates.extend(
            cube_plugin.parent.glob(
                "com.st.stm32cube.ide.mcu.debug.openocd_*/resources/openocd/st_scripts"
            )
        )

    # STM32CubeIDE keeps the executable and ST scripts in separate plugins.
    for root in cubeide_install_roots():
        candidates.extend(
            root.glob(
                "**/plugins/com.st.stm32cube.ide.mcu.debug.openocd_*/resources/openocd/st_scripts"
            )
        )

    valid = [
        path.resolve()
        for path in candidates
        if (path / "interface" / "cmsis-dap.cfg").is_file()
        and (path / "target" / "stm32wlx.cfg").is_file()
    ]
    return max(valid, key=lambda path: path.stat().st_mtime) if valid else None


def registry_subkeys(key: winreg.HKEYType) -> list[str]:
    names: list[str] = []
    index = 0
    while True:
        try:
            names.append(winreg.EnumKey(key, index))
            index += 1
        except OSError:
            return names


def registry_value(key: winreg.HKEYType, name: str) -> str | None:
    try:
        value, _ = winreg.QueryValueEx(key, name)
        return str(value)
    except OSError:
        return None


def device_is_connected(instance_id: str) -> bool:
    """Return True when the Windows PnP device is currently present."""
    cfgmgr32 = ctypes.WinDLL("cfgmgr32")
    locate = cfgmgr32.CM_Locate_DevNodeW
    locate.argtypes = [ctypes.POINTER(wintypes.ULONG), wintypes.LPCWSTR, wintypes.ULONG]
    locate.restype = wintypes.ULONG

    devinst = wintypes.ULONG()
    return locate(ctypes.byref(devinst), instance_id, 0) == 0


def com_port_sort_key(port_name: str) -> tuple[int, str]:
    """Sort COM ports by their numeric suffix, with a stable text fallback."""
    match = re.fullmatch(r"COM(\d+)", port_name, flags=re.IGNORECASE)
    return (int(match.group(1)), port_name) if match else (2**31 - 1, port_name)


def connected_programmers() -> list[tuple[str, list[str]]]:
    """Find connected dap42 USB serial numbers and all their CDC COM ports."""
    by_container: dict[str, list[str]] = {}
    programmers: list[tuple[str, list[str]]] = []

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, USB_REGISTRY_PATH) as usb_key:
        # Find the COM-port interface of each dap42 composite USB device.
        for device_key_name in registry_subkeys(usb_key):
            if not device_key_name.upper().startswith(DAP42_DEVICE_ID + "&MI_"):
                continue

            with winreg.OpenKey(usb_key, device_key_name) as interface_key:
                for instance_name in registry_subkeys(interface_key):
                    with winreg.OpenKey(interface_key, instance_name) as instance_key:
                        container_id = registry_value(instance_key, "ContainerID")
                        try:
                            with winreg.OpenKey(instance_key, "Device Parameters") as params_key:
                                port_name = registry_value(params_key, "PortName")
                        except OSError:
                            port_name = None

                        if container_id and port_name and port_name.upper().startswith("COM"):
                            ports = by_container.setdefault(container_id.upper(), [])
                            normalized_port = port_name.upper()
                            if normalized_port not in ports:
                                ports.append(normalized_port)

        # The parent composite-device instance name is the dap42 USB serial.
        with winreg.OpenKey(usb_key, DAP42_DEVICE_ID) as parent_key:
            for serial in registry_subkeys(parent_key):
                instance_id = f"USB\\{DAP42_DEVICE_ID}\\{serial}"
                if not device_is_connected(instance_id):
                    continue

                with winreg.OpenKey(parent_key, serial) as instance_key:
                    container_id = registry_value(instance_key, "ContainerID")

                com_ports = by_container.get(container_id.upper(), []) if container_id else []
                programmers.append((serial, sorted(com_ports, key=com_port_sort_key)))

    return sorted(programmers)


def print_connected_programmers(
    programmers: list[tuple[str, list[str]]] | None = None,
) -> None:
    print("Connected CMSIS-DAP programmers:")
    if programmers is None:
        try:
            programmers = connected_programmers()
        except OSError as error:
            print(f"  Unable to enumerate devices: {error}\n")
            return

    if not programmers:
        print("  none\n")
        return

    for serial, com_ports in programmers:
        ports_text = ", ".join(com_ports) if com_ports else "no COM ports"
        print(f"  {serial}  ->  {ports_text}")
    print()


def make_command(
    serial: str,
    firmware: Path,
    openocd: Path,
    scripts: Path,
    speed_khz: int,
    safe_mode: bool,
    run_after_program: bool,
) -> list[str]:
    firmware_for_tcl = firmware.resolve().as_posix()

    if firmware.suffix.lower() == ".bin":
        program = f"program {{{firmware_for_tcl}}} {BIN_ADDRESS} verify"
    else:
        program = f"program {{{firmware_for_tcl}}} verify"

    post_program = "reset run" if run_after_program else "reset halt"

    return [
        str(openocd),
        "-s",
        str(scripts),
        "-f",
        "interface/cmsis-dap.cfg",
        "-c",
        "cmsis-dap backend usb_bulk",
        *( ["-c", "cmsis-dap quirk enable"] if safe_mode else [] ),
        "-c",
        f"adapter serial {serial}",
        "-c",
        "transport select swd",
        "-f",
        "target/stm32wlx.cfg",
        "-c",
        f"adapter speed {speed_khz}",
        "-c",
        "gdb port disabled",
        "-c",
        "tcl port disabled",
        "-c",
        "telnet port disabled",
        "-c",
        program,
        "-c",
        post_program,
        "-c",
        "shutdown",
    ]


def output_reports_rdp_level_1(output: str) -> bool:
    """Return True only for the recoverable STM32 readout-protection level."""
    return "rdp level 1" in output.lower()


def make_rdp_unlock_command(
    serial: str,
    openocd: Path,
    scripts: Path,
    speed_khz: int,
    safe_mode: bool,
) -> list[str]:
    """Build an OpenOCD command that removes RDP level 1.

    Changing RDP from level 1 to level 0 causes a hardware-enforced mass erase.
    The option bytes are loaded immediately so a new OpenOCD process can program
    the now-unprotected device.
    """
    return [
        str(openocd),
        "-s",
        str(scripts),
        "-f",
        "interface/cmsis-dap.cfg",
        "-c",
        "cmsis-dap backend usb_bulk",
        *(["-c", "cmsis-dap quirk enable"] if safe_mode else []),
        "-c",
        f"adapter serial {serial}",
        "-c",
        "transport select swd",
        "-f",
        "target/stm32wlx.cfg",
        "-c",
        f"adapter speed {speed_khz}",
        "-c",
        "gdb port disabled",
        "-c",
        "tcl port disabled",
        "-c",
        "telnet port disabled",
        "-c",
        "init",
        "-c",
        "reset halt",
        "-c",
        "stm32l4x unlock 0",
        "-c",
        "stm32l4x option_load 0",
        "-c",
        "shutdown",
    ]


def remove_rdp_level_1(
    serial: str,
    openocd: Path,
    scripts: Path,
    speed_khz: int,
    safe_mode: bool,
) -> tuple[int, str]:
    """Remove RDP level 1 and return the OpenOCD result."""
    try:
        completed = subprocess.run(
            make_rdp_unlock_command(
                serial, openocd, scripts, speed_khz, safe_mode
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except OSError as error:
        return -1, str(error)
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        return -1, f"RDP unlock timed out after 60 seconds.\n{output}"
    output_lower = completed.stdout.lower()
    if "failed to unlock device" in output_lower or "option load failed" in output_lower:
        return completed.returncode or 1, completed.stdout
    return completed.returncode, completed.stdout


def release_target_reset(
    serial: str, openocd: Path, scripts: Path
) -> tuple[bool, str]:
    """Release nRESET without examining the target after a failed session."""
    command = [
        str(openocd),
        "-s",
        str(scripts),
        "-f",
        "interface/cmsis-dap.cfg",
        "-c",
        "cmsis-dap backend usb_bulk",
        "-c",
        f"adapter serial {serial}",
        "-c",
        "transport select swd",
        "-c",
        "adapter speed 100",
        "-c",
        "init",
        "-c",
        "cmsis-dap cmd 0x10 0x80 0x80 0 0 0 0",
        "-c",
        "shutdown",
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    return completed.returncode == 0, completed.stdout


def main() -> int:
    args = parse_args()
    firmware = args.firmware
    if args.speed <= 0:
        print("--speed must be greater than zero")
        return 2
    if not firmware.is_file():
        print(f"Firmware file not found: {firmware}")
        return 2

    openocd = find_openocd(args.openocd)
    if openocd is None:
        print("OpenOCD not found.")
        print("Install STM32CubeIDE, add OpenOCD to PATH, or use --openocd PATH.")
        print("You can also set OPENOCD or STM32CUBEIDE_PATH.")
        return 2

    scripts = find_scripts(args.scripts, openocd)
    if scripts is None:
        print(f"OpenOCD found: {openocd}")
        print("Compatible OpenOCD scripts not found (stm32wlx.cfg is required).")
        print("Use --scripts PATH or set OPENOCD_SCRIPTS.")
        return 2

    try:
        programmers = connected_programmers()
    except OSError as error:
        print(f"Unable to enumerate CMSIS-DAP programmers: {error}")
        return 2

    print_connected_programmers(programmers)

    connected_serials = [serial for serial, _ in programmers]
    serials = [value.strip() for value in args.serial if value.strip()]
    if not serials:
        serials = connected_serials

    if not serials:
        print("No connected DAP42 programmers found.")
        print("Connect a programmer or specify one with --serial SERIAL.")
        return 2

    if len(serials) != len(set(serials)):
        print("Duplicate --serial values were specified.")
        return 2

    excluded_serials = {value.strip() for value in args.exclude_serial if value.strip()}
    serials = [serial for serial in serials if serial not in excluded_serials]
    if not serials:
        print("No programmers left after applying --exclude-serial.")
        return 2

    processes: list[tuple[str, subprocess.Popen[str]]] = []

    print(f"OpenOCD: {openocd}")
    print(f"Scripts: {scripts}")
    print(f"Firmware: {firmware.resolve()}")
    print(f"Starting {len(serials)} programmers in parallel...\n")

    try:
        for serial in serials:
            print(f"[{serial}] starting")
            process = subprocess.Popen(
                make_command(
                    serial,
                    firmware,
                    openocd,
                    scripts,
                    args.speed,
                    args.safe,
                    not args.no_run,
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            processes.append((serial, process))

        results: list[tuple[str, int, str]] = []
        for serial, process in processes:
            output, _ = process.communicate()
            results.append((serial, process.returncode, output))

    except KeyboardInterrupt:
        print("\nInterrupted. Terminating OpenOCD processes...")
        for _, process in processes:
            if process.poll() is None:
                process.terminate()
        for _, process in processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        return 130

    final_results: list[tuple[str, int, str, str | None]] = []
    print()
    for serial, return_code, output in results:
        recovery_status: str | None = None
        if return_code != 0 and output_reports_rdp_level_1(output):
            print(f"[{serial}] RDP level 1 detected.")
            print(f"[{serial}] Removing protection (this mass-erases target flash)...")
            unlock_code, unlock_output = remove_rdp_level_1(
                serial, openocd, scripts, args.speed, args.safe
            )
            if unlock_code == 0:
                recovery_status = "RDP unlocked"
                print(f"[{serial}] RDP protection removed; retrying programming...")
                try:
                    retry = subprocess.run(
                        make_command(
                            serial,
                            firmware,
                            openocd,
                            scripts,
                            args.speed,
                            args.safe,
                            not args.no_run,
                        ),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=False,
                    )
                    return_code = retry.returncode
                    output = retry.stdout
                except OSError as error:
                    return_code = -1
                    output = str(error)
            else:
                recovery_status = "RDP unlock failed"
                return_code = unlock_code
                output = f"{output.rstrip()}\n\nRDP unlock output:\n{unlock_output}"

        final_results.append((serial, return_code, output, recovery_status))

    failed = False
    print()
    for serial, return_code, output, recovery_status in final_results:
        if return_code == 0:
            suffix = " (RDP protection removed)" if recovery_status else ""
            print(f"[{serial}] OK{suffix}")
        else:
            failed = True
            print(f"[{serial}] FAILED, OpenOCD exit code {return_code}")
            print(output.rstrip())
            released, release_output = release_target_reset(
                serial, openocd, scripts
            )
            if released:
                print(f"[{serial}] Target RESET released after failure.")
            else:
                print(f"[{serial}] WARNING: could not release target RESET.")
                print(release_output.rstrip())
            print()

    ports_by_serial = dict(programmers)
    print("Programmer summary:")
    for serial, return_code, _, recovery_status in final_results:
        com_ports = ports_by_serial.get(serial, [])
        ports_text = ", ".join(com_ports) if com_ports else "no COM ports"
        if return_code == 0:
            status = "OK (RDP protection removed)" if recovery_status else "OK"
        elif recovery_status == "RDP unlock failed":
            status = f"FAILED (RDP unlock failed, OpenOCD exit code {return_code})"
        elif recovery_status:
            status = f"FAILED after RDP unlock (OpenOCD exit code {return_code})"
        else:
            status = f"FAILED (OpenOCD exit code {return_code})"
        print(f"  {serial}  ->  {ports_text}  ->  {status}")
    print()

    if failed:
        print("One or more programmers failed.")
        return 1

    print("All programmers completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
