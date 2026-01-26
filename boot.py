# boot.py - ESP32启动配置
# 禁用REPL以便USB串口可被程序使用

import micropython
import os

# 禁用Ctrl+C中断，防止打断程序运行
micropython.kbd_intr(-1)

# 禁用USB REPL
import sys
# sys.stdout 和 sys.stdin 将被程序接管

print("boot.py: REPL已禁用，USB串口模式启用")
print("注意: 如需恢复REPL调试，请删除boot.py文件")
