from machine import Pin, Timer
from time import sleep_ms
import ubluetooth
import json
import os
import gc

# 配置参数
BLE_DEVICE_NAME = "ESP32-CodeLoader"  # 与 index.html 匹配
MTU_SIZE = 512

class ESP32_BLE():
    def __init__(self, name):
        self.led = Pin(4, Pin.OUT)
        self.timer1 = Timer(0)
        self.name = name
        self.ble = ubluetooth.BLE()
        self.ble.active(True)
        sleep_ms(100)  # 等待 BLE 初始化完成
        self.ble.config(gap_name=name)
        self.disconnected()
        self.ble.irq(self.ble_irq)
        self.register()
        sleep_ms(100)  # 等待服务注册完成
        self.advertiser()

        # 文件传输状态
        self.receiving_file = False
        self.current_filename = None
        self.file_buffer = bytearray()
        self.expected_size = 0
        self.conn_handle = None  # 改为 None 表示未连接
        self.is_connected = False  # 添加连接状态标志
        self.need_advertise = False  # 需要重新广播的标志

        print("ESP32代码执行器已启动")
        print("可用命令: run, save, list, delete, reboot, info")

    def connected(self):
        """连接成功：LED常亮"""
        print("蓝牙已连接")
        self.led.value(1)
        self.timer1.deinit()
        self.send_response("CONNECTED", "欢迎使用ESP32代码执行器")

    def disconnected(self):
        """断开连接：LED闪烁"""
        print("蓝牙已断开")
        self.timer1.init(period=100, mode=Timer.PERIODIC, callback=lambda t: self.led.value(not self.led.value()))

    def ble_irq(self, event, data):
        """蓝牙中断回调"""
        if event == 1:  # _IRQ_CENTRAL_CONNECT
            self.conn_handle = data[0]
            self.is_connected = True
            print(f"conn_handle: {self.conn_handle}, is_connected: {self.is_connected}")
            self.connected()

        elif event == 2:  # _IRQ_CENTRAL_DISCONNECT
            self.conn_handle = None
            self.is_connected = False
            self.need_advertise = True  # 设置标志，在主循环中重新广播
            self.disconnected()

        elif event == 3:  # _IRQ_GATTS_WRITE
            conn_handle, attr_handle = data

            if attr_handle == self.rx:
                # 接收命令
                buffer = self.ble.gatts_read(self.rx)
                self.process_command(buffer)

            elif attr_handle == self.code:
                # 接收大文件数据
                buffer = self.ble.gatts_read(self.code)
                self.process_code_data(buffer)

    def register(self):
        """注册BLE服务和特征值 - 4个特征匹配 index.html"""
        service_uuid = ubluetooth.UUID('6E400001-B5A3-F393-E0A9-E50E24DCCA9E')
        rx_uuid = ubluetooth.UUID('6E400002-B5A3-F393-E0A9-E50E24DCCA9E')  # 接收命令
        tx_uuid = ubluetooth.UUID('6E400003-B5A3-F393-E0A9-E50E24DCCA9E')  # 发送响应
        code_uuid = ubluetooth.UUID('6E400004-B5A3-F393-E0A9-E50E24DCCA9E')  # 大文件传输

        # 定义特征
        rx_char = (rx_uuid, ubluetooth.FLAG_WRITE | ubluetooth.FLAG_WRITE_NO_RESPONSE)
        tx_char = (tx_uuid, ubluetooth.FLAG_NOTIFY)
        code_char = (code_uuid, ubluetooth.FLAG_WRITE | ubluetooth.FLAG_NOTIFY)

        service = (service_uuid, (rx_char, tx_char, code_char))
        services = (service,)

        try:
            handles = self.ble.gatts_register_services(services)
            print("服务注册返回值：", handles)

            self.rx = handles[0][0]    # RX句柄
            self.tx = handles[0][1]    # TX句柄
            self.code = handles[0][2]  # CODE句柄
            print(f"RX句柄: {self.rx}, TX句柄: {self.tx}, CODE句柄: {self.code}")

            # 设置缓冲区
            self.ble.gatts_set_buffer(self.rx, 512, True)
            self.ble.gatts_set_buffer(self.code, 1024, True)

        except Exception as e:
            print("服务注册失败：", e)
            raise

    def send_response(self, status, message="", data=None):
        """发送JSON响应到客户端"""
        if not self.is_connected:
            print("警告：未连接，无法发送响应")
            return

        response = {
            "status": status,
            "message": message,
            "data": data
        }

        try:
            response_str = json.dumps(response)
            print(f"发送响应: {response_str[:100]}...")  # 只打印前100字符
            self.ble.gatts_notify(self.conn_handle, self.tx, response_str.encode('utf-8'))
        except Exception as e:
            print(f"发送响应失败: {e}")

    def advertiser(self):
        """启动BLE广播"""
        name = bytes(self.name, 'UTF-8')
        adv_prefix = bytearray('\x02\x01\x02', 'utf-8')
        adv_name_part = bytearray((len(name) + 1, 0x09)) + name
        adv_data = adv_prefix + adv_name_part
        self.ble.gap_advertise(100, adv_data)
        print("BLE广播已启动，设备名:", self.name)

    def process_command(self, data):
        """处理JSON命令"""
        try:
            command = json.loads(data.decode('utf-8'))
            cmd_type = command.get('type')
            print(f"收到命令: {cmd_type}")

            if cmd_type == 'run':
                self.run_code(command)
            elif cmd_type == 'save':
                self.save_code(command)
            elif cmd_type == 'list':
                self.list_files()
            elif cmd_type == 'delete':
                self.delete_file(command)
            elif cmd_type == 'reboot':
                self.reboot_esp()
            elif cmd_type == 'info':
                self.get_system_info()
            elif cmd_type == 'start_upload':
                self.start_file_upload(command)
            else:
                self.send_response("ERROR", f"未知命令: {cmd_type}")

        except Exception as e:
            self.send_response("ERROR", f"命令处理失败: {str(e)}")

    def start_file_upload(self, command):
        """开始文件上传"""
        filename = command.get('filename')
        total_size = command.get('size', 0)
        print(f"开始上传 - 文件名: {filename}, 大小: {total_size}")

        if not filename:
            self.send_response("ERROR", "需要文件名")
            return

        self.receiving_file = True
        self.current_filename = filename
        self.file_buffer = bytearray()
        self.expected_size = total_size

        print("发送 READY 响应...")
        self.send_response("READY", f"准备接收文件: {filename}", {
            "filename": filename,
            "buffer_size": 0
        })
        print("READY 响应已发送")

    def process_code_data(self, data):
        """处理代码数据块"""
        if not self.receiving_file:
            self.send_response("ERROR", "未处于接收状态")
            return

        self.file_buffer.extend(data)
        progress = (len(self.file_buffer) / self.expected_size * 100) if self.expected_size > 0 else 0

        if len(self.file_buffer) >= self.expected_size:
            self.save_complete_file()
        else:
            self.send_response("PROGRESS", "接收中...", {
                "received": len(self.file_buffer),
                "total": self.expected_size,
                "progress": progress
            })

    def save_complete_file(self):
        """保存完整的文件"""
        try:
            with open(self.current_filename, 'wb') as f:
                f.write(self.file_buffer)

            self.send_response("SUCCESS", f"文件保存成功: {self.current_filename}", {
                "filename": self.current_filename,
                "size": len(self.file_buffer)
            })
        except Exception as e:
            self.send_response("ERROR", f"文件保存失败: {str(e)}")
        finally:
            self.receiving_file = False
            self.current_filename = None
            self.file_buffer = bytearray()

    def run_code(self, command):
        """执行代码"""
        code = command.get('code')
        filename = command.get('filename')
        print(f"执行代码 - 代码长度: {len(code) if code else 0}, 文件名: {filename}")

        try:
            if code:
                # 检查是否有无限循环
                if 'while True' in code or 'while 1' in code:
                    print("警告：代码包含无限循环，可能会阻塞！")
                print("开始执行代码...")
                result = self.execute_code(code)
                print("代码执行完成")
                self.send_response("SUCCESS", "代码执行完成", {
                    "result": result,
                    "type": "direct"
                })
            elif filename:
                result = self.execute_file(filename)
                self.send_response("SUCCESS", f"文件执行完成: {filename}", {
                    "result": result,
                    "filename": filename
                })
            else:
                self.send_response("ERROR", "需要代码或文件名")
        except Exception as e:
            print(f"执行异常: {e}")
            self.send_response("ERROR", f"执行失败: {str(e)}")

    def execute_code(self, code):
        """执行代码字符串"""
        import machine
        import time
        # MicroPython 兼容的执行环境
        exec_globals = {
            'print': print,
            'Pin': Pin,
            'Timer': Timer,
            'machine': machine,
            'time': time,
            'os': os,
            'gc': gc
        }

        exec(code, exec_globals)

        return {
            "output": "代码执行成功",
            "memory_free": gc.mem_free(),
            "memory_alloc": gc.mem_alloc()
        }

    def execute_file(self, filename):
        """执行文件"""
        if not self.file_exists(filename):
            raise Exception(f"文件不存在: {filename}")

        with open(filename, 'r') as f:
            code = f.read()
        return self.execute_code(code)

    def save_code(self, command):
        """保存代码到文件"""
        code = command.get('code')
        filename = command.get('filename')
        print(f"保存请求 - 文件名: {filename}, 代码长度: {len(code) if code else 0}")

        if not code or not filename:
            print("错误：缺少代码或文件名")
            self.send_response("ERROR", "需要代码和文件名")
            return

        try:
            with open(filename, 'w') as f:
                f.write(code)
            print(f"文件保存成功: {filename}")
            self.send_response("SUCCESS", f"代码保存成功: {filename}", {
                "filename": filename,
                "size": len(code)
            })
        except Exception as e:
            print(f"保存异常: {e}")
            self.send_response("ERROR", f"保存失败: {str(e)}")

    def list_files(self):
        """列出所有文件"""
        try:
            files = []
            for file in os.listdir():
                if file.endswith('.py') or file.endswith('.txt'):
                    size = os.stat(file)[6]
                    files.append({"name": file, "size": size})

            self.send_response("SUCCESS", f"找到 {len(files)} 个文件", {"files": files})
        except Exception as e:
            self.send_response("ERROR", f"列出文件失败: {str(e)}")

    def delete_file(self, command):
        """删除文件"""
        filename = command.get('filename')

        if not filename:
            self.send_response("ERROR", "需要文件名")
            return

        try:
            if self.file_exists(filename):
                os.remove(filename)
                self.send_response("SUCCESS", f"文件已删除: {filename}")
            else:
                self.send_response("ERROR", f"文件不存在: {filename}")
        except Exception as e:
            self.send_response("ERROR", f"删除失败: {str(e)}")

    def reboot_esp(self):
        """重启ESP32"""
        import machine
        self.send_response("INFO", "正在重启...")
        machine.reset()

    def get_system_info(self):
        """获取系统信息"""
        info = {
            "platform": os.uname()[0],
            "version": os.uname()[3],
            "memory_free": gc.mem_free(),
            "memory_alloc": gc.mem_alloc(),
            "fs_free": self.get_fs_free(),
            "fs_total": self.get_fs_total()
        }
        self.send_response("SUCCESS", "系统信息", info)

    def file_exists(self, filename):
        """检查文件是否存在"""
        try:
            os.stat(filename)
            return True
        except OSError:
            return False

    def get_fs_free(self):
        """获取文件系统剩余空间"""
        try:
            fs_stat = os.statvfs('/')
            return fs_stat[0] * fs_stat[3]
        except:
            return 0

    def get_fs_total(self):
        """获取文件系统总空间"""
        try:
            fs_stat = os.statvfs('/')
            return fs_stat[0] * fs_stat[2]
        except:
            return 0


# 按键中断处理
button_flag = False
def buttons_irq(pin):
    global button_flag
    button_flag = True


if __name__ == "__main__":
    # 初始化BLE
    ble = ESP32_BLE(BLE_DEVICE_NAME)

    # 初始化按键（GPIO26）
    but = Pin(26, Pin.IN, Pin.PULL_UP)
    but.irq(trigger=Pin.IRQ_FALLING, handler=buttons_irq)

    # 初始化LED
    led = Pin(4, Pin.OUT)
    led15 = Pin(15, Pin.OUT)
    led15.value(0)

    # 主循环
    while True:
        # 检查是否需要重新广播
        if ble.need_advertise:
            ble.need_advertise = False
            sleep_ms(100)  # 等待 BLE 状态稳定
            try:
                ble.advertiser()
            except OSError as e:
                print(f"重新广播失败: {e}")

        if button_flag:
            button_flag = False
            led.value(not led.value())
            status = 'LED is ON.' if led.value() else 'LED is OFF'
            print("按键触发：", status)
            ble.send_response("INFO", status)

        sleep_ms(100)
