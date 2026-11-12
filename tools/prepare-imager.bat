@echo off
setlocal
set "IMAGE="
for %%I in (*.img.xz) do (
    if defined IMAGE (
        echo More than one .img.xz image found. Run this helper in a folder with one PetCam image.
        exit /b 1
    )
    set "IMAGE=%%I"
)
if not defined IMAGE (
    echo No .img.xz image found in this folder.
    exit /b 1
)
py -3 "%~dp0create_imager_manifest.py" "%CD%\%IMAGE%"
if errorlevel 1 exit /b %errorlevel%

set "IMAGER="
for %%P in (
    "%ProgramFiles%\Raspberry Pi Imager\Raspberry Pi Imager.exe"
    "%ProgramFiles(x86)%\Raspberry Pi Imager\Raspberry Pi Imager.exe"
) do if exist "%%~P" set "IMAGER=%%~P"
if not defined IMAGER (
    for /f "delims=" %%P in ('where rpi-imager.exe 2^>nul') do if not defined IMAGER set "IMAGER=%%P"
)
if not defined IMAGER (
    echo PetCam manifest created: %CD%\petcam-os.rpi-imager-manifest
    echo Could not locate Raspberry Pi Imager. Install Imager 2, then open it and select:
    echo App Options ^> Content Repository ^> Edit ^> Use custom file.
    pause
    exit /b 2
)
start "" "%IMAGER%" --repo "%CD%\petcam-os.rpi-imager-manifest"
