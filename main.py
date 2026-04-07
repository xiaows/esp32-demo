"""
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

    # 初始化LED
    led = Pin(4, Pin.OUT)
    led15 = Pin(15, Pin.OUT)
    led15.value(0)

    print("ESP32代码执行器已启动")
    print("支持通信方式: BLE, USB")

    # 检查自启动
    import _autorun
    _autorun.check_and_run(ble._run_code_thread)

    # 主循环
    while True:
        # 发送子线程积累的 BLE 响应（线程安全）
        ble.flush_responses()

        # 断连后清理（从 IRQ 移到主循环，避免在中断上下文做重活）
        if ble.need_cleanup:
            ble.need_cleanup = False
            ble.receiving_file = False
            ble.receiving_code = False
            ble.file_buffer = bytearray()
            ble._resp_queue.clear()
            gc.collect()
            print(f"[BLE] 断连清理完成, 可用内存: {gc.mem_free()}")

        # 检查是否需要重新广播（BLE）
        if ble.need_advertise:
            ble.need_advertise = False
            # 先停止旧广播，等双方 BLE 栈完全释放旧连接
            try:
                ble.ble.gap_advertise(None)
            except:
                pass
            sleep_ms(2000)
            gc.collect()
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
