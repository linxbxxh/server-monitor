' 隐藏窗口启动 ServerMonitor（悬浮卡片本体不带控制台）
' 输出重定向到 ServerMonitor.out.log，避免隐藏后丢失日志
Option Explicit
Dim shell, exePath, logPath
Set shell = CreateObject("WScript.Shell")
exePath = "C:\Users\linxb\.zcode\workspace\default\server-monitor\ServerMonitor.exe"
logPath = "C:\Users\linxb\.zcode\workspace\default\server-monitor\ServerMonitor.out.log"
shell.CurrentDirectory = "C:\Users\linxb\.zcode\workspace\default\server-monitor"
shell.Run "cmd.exe /c """"" & exePath & """ >> """ & logPath & """ 2>&1""", 0, False
