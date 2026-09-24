# Parallel flashing with `flash_all.py`

`flash_all.py` flashes one firmware image to one or more STM32WL targets in
parallel. Each target must be connected to a separate DAP42 CMSIS-DAP probe.

The script is intended for Windows: it discovers connected DAP42 probes in the
Windows USB registry and starts a separate OpenOCD process for every selected
probe.

## Requirements

- Windows 10 or 11.
- Python 3.10 or newer.
- One or more DAP42 probes connected by USB.
- Each probe connected to a separate target through SWD (`SWDIO`, `SWCLK`,
  `GND`, and, preferably, `nRESET`).
- An ELF, HEX, or BIN firmware file for the target.
- OpenOCD with these scripts:
  - `interface/cmsis-dap.cfg`
  - `target/stm32wlx.cfg`

A compatible Windows OpenOCD distribution is included in the `openocd`
subdirectory. The script can also use OpenOCD from `PATH` or STM32CubeIDE.

## Quick start

Open PowerShell or Command Prompt in this directory and run:

```powershell
python flash_all.py path\to\firmware.elf
```

All connected DAP42 probes are detected automatically and programmed in
parallel. ELF and HEX files contain their own load addresses. A BIN file is
always written starting at `0x08000000`.

Example for a BIN image:

```powershell
python flash_all.py D:\firmware\app.bin
```

The script verifies every programmed image and resets each successfully
programmed target into run mode.

If an STM32WL target reports RDP level 1, the script automatically removes the
readout protection and retries programming once. Removing RDP level 1 causes a
hardware-enforced mass erase of the target flash. RDP level 2 is not changed.
The final summary marks a recovered target as `OK (RDP protection removed)`.

## Selecting probes

Without `--serial`, all connected DAP42 probes are used. To program only
specific probes, repeat `--serial` for every required USB serial number:

```powershell
python flash_all.py app.elf --serial DAP42-001
python flash_all.py app.elf --serial DAP42-001 --serial DAP42-002
```

At startup the script prints detected serial numbers and their associated COM
ports. DAP103 BluePill firmware exposes two CDC interfaces, so both ports are
listed when available. The COM ports are shown for identification only;
flashing uses the CMSIS-DAP USB interface.

Example discovery output:

```text
Connected CMSIS-DAP programmers:
  353503677089575455FF6A06  ->  COM117, COM119
  5AC01A434E57343637FF7106  ->  COM120, COM121
```

To skip one or more probes, repeat `--exclude-serial`:

```powershell
python flash_all.py app.elf --exclude-serial 353503677089575455FF6A06
```

## Options

```text
python flash_all.py FIRMWARE [options]

--serial SERIAL   Select a probe by USB serial number. May be repeated.
--exclude-serial SERIAL
                  Skip a probe by USB serial number. May be repeated.
--openocd PATH    Path to openocd.exe.
--scripts PATH    Path to the OpenOCD scripts directory.
--speed KHZ       Initial SWD speed in kHz. Default: 500.
--safe            Enable the one-packet CMSIS-DAP compatibility mode.
--no-run          Leave targets halted after programming.
-h, --help        Show command-line help.
```

Examples:

```powershell
# Use a slower SWD clock for a long or noisy connection
python flash_all.py app.elf --speed 100

# Compatibility mode for probes or USB connections with transfer problems
python flash_all.py app.elf --safe

# Program and verify, but leave the MCU halted
python flash_all.py app.elf --no-run

# Program all connected targets except the central module
python flash_all.py app.elf `
  --exclude-serial 353503677089575455FF6A06

# Explicit OpenOCD installation
python flash_all.py app.elf `
  --openocd C:\OpenOCD\bin\openocd.exe `
  --scripts C:\OpenOCD\share\openocd\scripts
```

## OpenOCD discovery

The executable is searched for in this order:

1. `--openocd PATH`.
2. The `OPENOCD` environment variable.
3. `openocd.exe` or `openocd` in `PATH`.
4. The bundled `openocd\bin\openocd.exe`.
5. Installed STM32CubeIDE directories.

The scripts directory is selected from `--scripts`, `OPENOCD_SCRIPTS`, common
OpenOCD layouts, or an STM32CubeIDE installation. A directory is accepted only
if it contains both `interface/cmsis-dap.cfg` and `target/stm32wlx.cfg`.

Environment variables can be set for the current PowerShell session:

```powershell
$env:OPENOCD = "C:\OpenOCD\bin\openocd.exe"
$env:OPENOCD_SCRIPTS = "C:\OpenOCD\share\openocd\scripts"
python flash_all.py app.elf
```

`STM32CUBEIDE_PATH` can be used to point the automatic search at a non-standard
STM32CubeIDE installation.

## Result and exit codes

After all programming sessions finish, the script prints the DAP42 serial,
associated COM ports, and final status for every selected probe:

```text
Programmer summary:
  353503677089575455FF6A06  ->  COM117, COM119  ->  OK
  5AC01A434E57343637FF7106  ->  COM120, COM121  ->  OK (RDP protection removed)
```

The script reports `OK` or `FAILED` for every probe and exits with:

- `0` — all selected targets were programmed and verified successfully.
- `1` — at least one OpenOCD programming session failed.
- `2` — invalid arguments, missing firmware/OpenOCD scripts, or no usable
  probes.
- `130` — interrupted with `Ctrl+C`.

If a programming session fails, the script makes a best-effort attempt to
release the target's `nRESET` line before exiting.

## Troubleshooting

### No connected DAP42 programmers found

- Check the USB cable and confirm that the DAP42 device appears in Device
  Manager.
- Disconnect and reconnect the probe.
- Make sure the probe firmware uses USB VID `1209` and PID `DA42`.
- Run `python flash_all.py --help` from a normal Windows terminal, not WSL.

### OpenOCD or scripts not found

Use `--openocd` and `--scripts` explicitly, or set `OPENOCD` and
`OPENOCD_SCRIPTS`. The scripts directory must contain `target/stm32wlx.cfg`.

### SWD connection or verification fails

- Check `SWDIO`, `SWCLK`, `GND`, target power, and `nRESET`.
- Verify that every probe is wired to only one target.
- Reduce the clock, for example with `--speed 100`.
- Retry with `--safe`.
- Avoid long SWD wires and connect the probe and target grounds directly.

### Target reports `RDP level 1` or `device protected`

The Wio-E5 factory AT firmware is normally supplied with RDP Level 1. The
script automatically runs `stm32l4x unlock 0`, reloads the option bytes, and
retries programming once. This operation mass-erases the target flash. The
batch files and the application firmware do not enable RDP.

If automatic recovery fails, connect with STM32CubeProgrammer and set RDP to
`AA` (Level 0). RDP Level 2 cannot be recovered and is not modified by the
script.

### Multiple probes have the same serial number

Every DAP42 probe should have a unique USB serial number. Duplicate serials
cannot be selected reliably by OpenOCD and must be corrected in the probe
firmware or USB configuration.

## Safety notes

- Confirm the firmware belongs to the connected STM32WL target before running
  the script.
- All selected targets are erased/programmed concurrently.
- Automatic RDP Level 1 removal mass-erases the complete target flash and
  permanently removes the existing factory AT firmware.
- BIN files do not contain an address; this script always writes them at
  `0x08000000`.
- Do not disconnect probes or remove target power while programming is in
  progress.
