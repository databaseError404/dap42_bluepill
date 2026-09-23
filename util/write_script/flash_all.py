#!/usr/bin/env python3
"""Parallel firmware flashing through one or more CMSIS-DAP probes.

Usage:
    python flash_all.py path\to\firmware.elf
    python flash_all.py path\to\firmware.bin
    python flash_all.py firmware.elf --serial SERIAL --serial SERIAL
"""

from __future__ import annotations

import argparse
import ctypes
import os
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


def connected_programmers() -> list[tuple[str, str | None]]:
    """Find connected dap42 USB serial numbers and their CDC COM ports."""
    by_container: dict[str, str] = {}
    programmers: list[tuple[str, str | None]] = []

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
                            by_container[container_id.upper()] = port_name.upper()

        # The parent composite-device instance name is the dap42 USB serial.
        with winreg.OpenKey(usb_key, DAP42_DEVICE_ID) as parent_key:
            for serial in registry_subkeys(parent_key):
                instance_id = f"USB\\{DAP42_DEVICE_ID}\\{serial}"
                if not device_is_connected(instance_id):
                    continue

                with winreg.OpenKey(parent_key, serial) as instance_key:
                    container_id = registry_value(instance_key, "ContainerID")

                com_port = by_container.get(container_id.upper()) if container_id else None
                programmers.append((serial, com_port))

    return sorted(programmers)


def print_connected_programmers(
    programmers: list[tuple[str, str | None]] | None = None,
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

    for serial, com_port in programmers:
        print(f"  {serial}  ->  {com_port or 'no COM port'}")
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

    failed = False
    print()
    for serial, return_code, output in results:
        if return_code == 0:
            print(f"[{serial}] OK")
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

    if failed:
        print("One or more programmers failed.")
        return 1

    print("All programmers completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
