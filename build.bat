@echo off
chcp 65001 >nul
cd /d %~dp0
pip install -r requirements.txt pyinstaller || goto :err
rem --exclude-module: conda 环境下 PyInstaller 会误带 MKL/Qt(Side6)/numpy 等大依赖, 本应用只用到 PyQt5
pyinstaller --onefile --noconfirm --name ServerMonitor ^
  --add-data "monitor/static;monitor/static" ^
  --exclude-module matplotlib --exclude-module numpy --exclude-module scipy ^
  --exclude-module pandas --exclude-module PIL --exclude-module tkinter ^
  --exclude-module PySide2 --exclude-module PySide6 ^
  --exclude-module torch --exclude-module IPython ^
  widget.py || goto :err
copy /y dist\ServerMonitor.exe ServerMonitor.exe
echo.
echo 构建完成: ServerMonitor.exe (双击运行桌面悬浮卡片, 需与 config.yaml 放在同一目录)
pause
exit /b 0
:err
echo 构建失败, 请检查上方错误信息
pause
exit /b 1
