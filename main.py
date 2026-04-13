"""
MicroPython v1.27.0 on 2025-12-09; Generic ESP32 module with ESP32
ESP32 代码执行器 - 主入口
支持BLE和USB两种通信方式

注意：USB模式需要配合boot.py禁用REPL
"""

from machine import Pin
from time import sleep_ms
import gc

from ble import ESP32_BLE, load_ble_name
from usb import ESP32_USB

# 配置参数
USB_ENABLED = True

# 按键中断处理
button_flag = False


def buttons_irq(pin):
    global button_flag
    button_flag = True


def main():
    global button_flag

    BLE_DEVICE_NAME = load_ble_name()

    # 初始化BLE
    ble = ESP32_BLE(BLE_DEVICE_NAME)

    # 初始化USB串口通信
    usb = None
    if USB_ENABLED:
        try:
            usb = ESP32_USB()
        except Exception as e:
            print(f"USB初始化失败: {e}")

    # 初始化按键（GPIO26）
    but = Pin(26, Pin.IN, Pin.PULL_UP)
    but.irq(trigger=Pin.IRQ_FALLING, handler=buttons_irq)

    MOTOR_EN = Pin(22, Pin.OUT)
    MOTOR_EN.value(1)
    BEEP_EN = Pin(4, Pin.OUT)
    BEEP_EN.value(0)
    print("ESP32代码执行器已启动")
    print("支持通信方式: BLE, USB")

    # 检查自启动
    import _autorun
    _autorun.check_and_run(ble._run_code_thread)

    # 主循环
    while True:
        # 处理 IRQ 缓存的事件和数据（命令、代码块等）
        ble.poll()

        # 发送子线程积累的 BLE 响应（线程安全）
        ble.flush_responses()

        # 断连后清理
        if ble.need_cleanup:
            ble.need_cleanup = False
            ble.receiving_file = False
            ble.receiving_code = False
            ble.file_buffer = bytearray()
            ble._resp_queue.clear()
            ble._cmd_queue.clear()
            ble._code_data_queue.clear()
            gc.collect()
            print(f"[BLE] 断连清理完成, 可用内存: {gc.mem_free()}")

        # 普通断连：只需要重新广播，无需重置协议栈
        if ble.need_readvertise:
            ble.need_readvertise = False
            sleep_ms(200)
            gc.collect()
            try:
                ble.advertiser()
            except OSError as e:
                print(f"[BLE] 重新广播失败，尝试完整重置: {e}")
                ble.need_advertise = True

        # 完整重置协议栈（仅改名等特殊情况，或广播失败时的降级）
        if ble.need_advertise:
            ble.need_advertise = False
            sleep_ms(500)
            gc.collect()
            ble.restart_ble()
            for _adv_retry in range(3):
                try:
                    ble.advertiser()
                    break
                except OSError as e:
                    print(f"重新广播失败(重试{_adv_retry+1}/3): {e}")
                    gc.collect()
                    sleep_ms(1000)

        # 检查USB输入
        if usb:
            usb.check_input()

        if button_flag:
            button_flag = False
            led.value(not led.value())
            status = 'LED is ON.' if led.value() else 'LED is OFF'
            print("按键触发：", status)
            ble.send_response("INFO", status)
            if usb:
                usb.send_response("INFO", status)

        sleep_ms(50)  # 稍微减少延迟


if __name__ == "__main__":
    main()