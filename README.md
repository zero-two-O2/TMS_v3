# Thermal Monitoring System V3

## Status

Architecture design phase.

## Primary Modes

- Configuration
- Observer

## Offline Analysis

Saved raw camera data can be loaded and analyzed without connecting to cameras.

## Technology

- Python 3.10
- HALCON
- PyQt6
- SQL Server
- Shared-memory frame transport



What happened

The PC had the correct native MVTec HALCON installation, but the Python environment initially had the wrong package.

Initial state

The PC had:

HALCON native installation:
C:\Program Files\MVTec\HALCON-24.11-Progress-Steady


Native version:
24.11.2.0


Python:
3.10.7

But the venv contained:

halcon 1.0.0

That is not MVTec HALCON. It was an unrelated Python package. Therefore:

import halcon

worked, but functions such as:

open_framegrabber
grab_image
gen_rectangle1

did not exist.

Correct fix

We removed the wrong package:

pip uninstall halcon

Then identified the native HALCON version:

Get-Content "C:\Program Files\MVTec\HALCON-24.11-Progress-Steady\version.txt"

Result:

24.11.2.0

The matching PyPI MVTec Python binding is:

mvtec-halcon==24112.0.0

Installed with:

pip install mvtec-halcon==24112.0.0

This installed the correct MVTec Python binding.

Second problem

After installing the correct binding, Python reported:

Unable to find any HALCON library.

The reason was PATH.

The native library existed:

C:\Program Files\MVTec\HALCON-24.11-Progress-Steady\bin\x64-win64\halcon.dll

but that directory was not in the Windows PATH.

So we set:

$env:HALCONROOT = "C:\Program Files\MVTec\HALCON-24.11-Progress-Steady"
$env:PATH = "$env:HALCONROOT\bin\x64-win64;$env:PATH"

After that, the Python binding can find halcon.dll.

Future PC setup procedure

Use this sequence whenever setting up another PC.

1. Check Python
python --version

For this project:

Python 3.10.x
2. Check native HALCON

Find the installation:

Get-ChildItem "C:\Program Files\MVTec"

Then:

Get-Content "C:\Program Files\MVTec\HALCON-24.11-Progress-Steady\version.txt"

Example:

24.11.2.0

Always check the native version first.

3. Remove the wrong halcon package if present
pip uninstall halcon

Do not install:

pip install halcon

That is not the MVTec package.

4. Install matching MVTec Python binding

For HALCON:

24.11.2.x

use:

pip install mvtec-halcon==24112.0.0

For future versions, use the corresponding MVTec Python binding rather than guessing.

Check available versions:

pip index versions mvtec-halcon
5. Set HALCON environment variables

For the current PowerShell session:

$env:HALCONROOT = "C:\Program Files\MVTec\HALCON-24.11-Progress-Steady"
$env:PATH = "$env:HALCONROOT\bin\x64-win64;$env:PATH"

Verify:

echo $env:HALCONROOT

and:

$env:PATH -split ";" | Where-Object { $_ -match "HALCON" }
6. Verify Python HALCON
python -c "import halcon as ha; print('HALCON:', ha.__file__); print('open_framegrabber:', hasattr(ha,'open_framegrabber')); print('grab_image:', hasattr(ha,'grab_image')); print('gen_rectangle1:', hasattr(ha,'gen_rectangle1'))"

Expected:

open_framegrabber: True
grab_image: True
gen_rectangle1: True
7. Verify the native DLL
Test-Path "$env:HALCONROOT\bin\x64-win64\halcon.dll"

Expected:

True
8. Only then test TMS

Set the camera:

$env:TV46L_DEVICE = "default"
$env:TV46L_SERIAL = "HB25100004"

Then:

python tests/test_real_ir_acquisition.py
Important distinction

There are three separate things:

MVTec HALCON installation
        ↓
C:\Program Files\MVTec\HALCON-24.11-Progress-Steady
        ↓
halcon.dll
Python binding
        ↓
pip install mvtec-halcon==24112.0.0
        ↓
import halcon
Windows PATH
        ↓
...HALCON...\bin\x64-win64
        ↓
Python binding can find halcon.dll

All three must be correct.

For this PC, the final known-good target is:

Python             3.10.7
Native HALCON      24.11.2.0
Python binding     mvtec-halcon 24112.0.0
HALCON DLL         present
HALCON DLL path    in PATH
GPU                RTX A1000
CUDA               working separately

This setup issue is PC environment configuration, not a V3 code problem.