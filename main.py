"""
ESP32 代码执行器 - 主入口
支持BLE和USB两种通信方式

注意：USB模式需要配合boot.py禁用REPL
"""

from machine import Pin
from time import sleep_ms, ticks_add, ticks_diff, ticks_ms
import gc
import sys

from ble import ESP32_BLE, load_ble_name
from usb import ESP32_USB

# 配置参数
USB_ENABLED = True
MAIN_LOOP_DELAY_MS = 50
MAIN_LOOP_ERROR_DELAY_MS = 250
BLE_READVERTISE_DELAY_MS = 2000
BLE_READVERTISE_RETRY_DELAY_MS = 1000
BLE_READVERTISE_BACKOFF_MS = 5000
BLE_READVERTISE_MAX_RETRIES = 3

# 按键中断处理
button_flag = False


def buttons_irq(pin):
    global button_flag
    button_flag = True


def _cleanup_ble_state(ble):
    """将断连后的资源清理放回主循环，避免在 IRQ 中做重活。"""
    ble.need_cleanup = False
    ble.receiving_file = False
    ble.receiving_code = False
    ble.file_buffer = bytearray()
    ble._resp_queue.clear()
    gc.collect()
    print(f"[BLE] 断连清理完成, 可用内存: {gc.mem_free()}")


def _schedule_ble_readvertise(ble):
    """停止旧广播并安排稍后重启，避免主循环被长时间 sleep 卡住。"""
    try:
        ble.ble.gap_advertise(None)
    except Exception:
        pass
    gc.collect()
    return ticks_add(ticks_ms(), BLE_READVERTISE_DELAY_MS), 0


def _service_ble_readvertise(ble, next_retry_at, retry_count):
    if next_retry_at is None or ticks_diff(ticks_ms(), next_retry_at) < 0:
        return next_retry_at, retry_count

    try:
        ble.advertiser()
        return None, 0
    except OSError as e:
        retry_count += 1
        delay_ms = BLE_READVERTISE_RETRY_DELAY_MS
        if retry_count >= BLE_READVERTISE_MAX_RETRIES:
            print(
                f"[BLE] 重广播连续失败{retry_count}次，"
                f"{BLE_READVERTISE_BACKOFF_MS}ms后进入下一轮重试: {e}"
            )
            retry_count = 0
            delay_ms = BLE_READVERTISE_BACKOFF_MS
        else:
            print(f"[BLE] 重广播失败(重试{retry_count}/{BLE_READVERTISE_MAX_RETRIES}): {e}")
        gc.collect()
        return ticks_add(ticks_ms(), delay_ms), retry_count


def _log_main_loop_exception(exc):
    print("[MAIN] 主循环异常，稍后继续运行")
    try:
        sys.print_exception(exc)
    except Exception:
        print(exc)


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
    advertise_retry_at = None
    advertise_retry_count = 0

    while True:
        try:
            # 发送子线程积累的 BLE 响应（线程安全）
            ble.flush_responses()

            # 断连后清理（从 IRQ 移到主循环，避免在中断上下文做重活）
            if ble.need_cleanup:
                _cleanup_ble_state(ble)

            # 检查是否需要重新广播（BLE）
            if ble.need_advertise:
                ble.need_advertise = False
                advertise_retry_at, advertise_retry_count = _schedule_ble_readvertise(ble)

            advertise_retry_at, advertise_retry_count = _service_ble_readvertise(
                ble, advertise_retry_at, advertise_retry_count
            )

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

            sleep_ms(MAIN_LOOP_DELAY_MS)
        except Exception as e:
            _log_main_loop_exception(e)
            gc.collect()
            sleep_ms(MAIN_LOOP_ERROR_DELAY_MS)


if __name__ == "__main__":
    main()
