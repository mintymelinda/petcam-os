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
py -3 create_imager_manifest.py "%IMAGE%"
if errorlevel 1 exit /b %errorlevel%
start "" "petcam-os.rpi-imager-manifest"
