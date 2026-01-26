"""
ESP32 代码执行器 - 主程序入口
支持BLE和USB两种通信方式
"""

from machine import Pin
from time import sleep_ms

# 配置参数
BLE_DEVICE_NAME = "ESP32-CodeLoader"
BLE_ENABLED = True
USB_ENABLED = True

# 按键中断处理
button_flag = False

def buttons_irq(pin):
    global button_flag
    button_flag = True


def main():
    global button_flag

    print("=" * 40)
    print("ESP32 代码执行器")
    print("=" * 40)

    # 初始化BLE
    ble = None
    if BLE_ENABLED:
        try:
            from ble import ESP32_BLE
            ble = ESP32_BLE(BLE_DEVICE_NAME)
            print("BLE模块已加载")
        except Exception as e:
            print(f"BLE初始化失败: {e}")

    # 初始化USB串口通信
    usb = None
    if USB_ENABLED:
        try:
            from usb import ESP32_USB
            usb = ESP32_USB()
            print("USB模块已加载")
        except Exception as e:
            print(f"USB初始化失败: {e}")

    # 初始化按键（GPIO26）
    but = Pin(26, Pin.IN, Pin.PULL_UP)
    but.irq(trigger=Pin.IRQ_FALLING, handler=buttons_irq)

    # 初始化LED
    led = Pin(4, Pin.OUT)
    led15 = Pin(15, Pin.OUT)
    led15.value(0)

    print("-" * 40)
    print("可用命令: run, save, list, delete, reboot, info, stop")
    print("等待连接...")
    print("-" * 40)

    # 主循环
    while True:
        # 检查BLE是否需要重新广播
        if ble and ble.need_advertise:
            ble.need_advertise = False
            sleep_ms(100)
            try:
                ble.advertiser()
            except OSError as e:
                print(f"BLE重新广播失败: {e}")

        # 检查USB输入
        if usb:
            usb.check_input()

        # 处理按键事件
        if button_flag:
            button_flag = False
            led.value(not led.value())
            status = 'LED is ON.' if led.value() else 'LED is OFF'
            print("按键触发：", status)

            if ble and ble.is_connected:
                ble.send_response("INFO", status)
            if usb:
                usb.send_response("INFO", status)

        sleep_ms(50)  # 稍微减少延迟


if __name__ == "__main__":
    main()
