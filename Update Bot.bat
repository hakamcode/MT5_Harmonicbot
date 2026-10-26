@echo off
cd /d "%~dp0"
echo Mengambil update terbaru dari GitHub...
git pull
echo.
echo Selesai. config.json Anda tidak diubah.
pause
