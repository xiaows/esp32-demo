"""
ESP32 BLE通信模块
通过蓝牙低功耗(BLE)接收命令和发送响应
"""

from machine import Pin, Timer
from time import sleep_ms
import ubluetooth
import json
import os
import gc
import _thread


class ESP32_BLE:
    def __init__(self, name, led_pin=4):
        self.led = Pin(led_pin, Pin.OUT)
        self.timer1 = Timer(0)
        self.name = name
        self.ble = ubluetooth.BLE()
        self.ble.active(True)
        sleep_ms(100)
        self.ble.config(gap_name=name)
        self.disconnected()
        self.ble.irq(self.ble_irq)
        self.register()
        sleep_ms(100)
        self.advertiser()

        # 文件传输状态
        self.receiving_file = False
        self.receiving_code = False
        self.current_filename = None
        self.file_buffer = bytearray()
        self.expected_size = 0
        self.conn_handle = None
        self.is_connected = False
        self.need_advertise = False
        self.code_running = False
        self.stop_flag = False

        print("BLE通信已启动，设备名:", name)

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
            self.connected()

        elif event == 2:  # _IRQ_CENTRAL_DISCONNECT
            self.conn_handle = None
            self.is_connected = False
            self.need_advertise = True
            self.disconnected()

        elif event == 3:  # _IRQ_GATTS_WRITE
            conn_handle, attr_handle = data

            if attr_handle == self.rx:
                buffer = self.ble.gatts_read(self.rx)
                self.process_command(buffer)

            elif attr_handle == self.code:
                buffer = self.ble.gatts_read(self.code)
                self.process_code_data(buffer)

    def register(self):
        """注册BLE服务和特征值"""
        service_uuid = ubluetooth.UUID('6E400001-B5A3-F393-E0A9-E50E24DCCA9E')
        rx_uuid = ubluetooth.UUID('6E400002-B5A3-F393-E0A9-E50E24DCCA9E')
        tx_uuid = ubluetooth.UUID('6E400003-B5A3-F393-E0A9-E50E24DCCA9E')
        code_uuid = ubluetooth.UUID('6E400004-B5A3-F393-E0A9-E50E24DCCA9E')

        rx_char = (rx_uuid, ubluetooth.FLAG_WRITE | ubluetooth.FLAG_WRITE_NO_RESPONSE)
        tx_char = (tx_uuid, ubluetooth.FLAG_NOTIFY)
        code_char = (code_uuid, ubluetooth.FLAG_WRITE | ubluetooth.FLAG_NOTIFY)

        service = (service_uuid, (rx_char, tx_char, code_char))
        services = (service,)

        try:
            handles = self.ble.gatts_register_services(services)
            self.rx = handles[0][0]
            self.tx = handles[0][1]
            self.code = handles[0][2]

            self.ble.gatts_set_buffer(self.rx, 512, True)
            self.ble.gatts_set_buffer(self.code, 1024, True)
        except Exception as e:
            print("BLE服务注册失败：", e)
            raise

    def send_response(self, status, message="", data=None):
        """发送JSON响应到客户端"""
        if not self.is_connected:
            return

        response = {"status": status, "message": message, "data": data}

        try:
            response_str = json.dumps(response)
            self.ble.gatts_notify(self.conn_handle, self.tx, response_str.encode('utf-8'))
        except Exception as e:
            print(f"BLE发送响应失败: {e}")

    def advertiser(self):
        """启动BLE广播"""
        name = bytes(self.name, 'UTF-8')
        adv_prefix = bytearray('\x02\x01\x02', 'utf-8')
        adv_name_part = bytearray((len(name) + 1, 0x09)) + name
        adv_data = adv_prefix + adv_name_part
        self.ble.gap_advertise(100, adv_data)
        print("BLE广播已启动")

    def process_command(self, data):
        """处理JSON命令"""
        try:
            command = json.loads(data.decode('utf-8'))
            cmd_type = command.get('type')
            print(f"BLE收到命令: {cmd_type}")

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
            elif cmd_type == 'start_run':
                self.start_code_run(command)
            elif cmd_type == 'stop':
                self.stop_code()
            else:
                self.send_response("ERROR", f"未知命令: {cmd_type}")

        except Exception as e:
            self.send_response("ERROR", f"命令处理失败: {str(e)}")

    def start_file_upload(self, command):
        """开始文件上传"""
        filename = command.get('filename')
        total_size = command.get('size', 0)

        if not filename:
            self.send_response("ERROR", "需要文件名")
            return

        self.receiving_file = True
        self.receiving_code = False
        self.current_filename = filename
        self.file_buffer = bytearray()
        self.expected_size = total_size

        self.send_response("READY", f"准备接收文件: {filename}", {
            "filename": filename,
            "buffer_size": 0
        })

    def start_code_run(self, command):
        """开始接收代码执行（分块传输）"""
        total_size = command.get('size', 0)

        if self.code_running:
            self.send_response("ERROR", "已有代码在运行，请先发送stop命令")
            return

        self.receiving_file = False
        self.receiving_code = True
        self.file_buffer = bytearray()
        self.expected_size = total_size

        self.send_response("READY_RUN", "准备接收代码", {"buffer_size": 0})

    def process_code_data(self, data):
        """处理代码数据块"""
        if not self.receiving_file and not self.receiving_code:
            self.send_response("ERROR", "未处于接收状态")
            return

        self.file_buffer.extend(data)
        progress = (len(self.file_buffer) / self.expected_size * 100) if self.expected_size > 0 else 0

        if len(self.file_buffer) >= self.expected_size:
            if self.receiving_file:
                self.save_complete_file()
            elif self.receiving_code:
                self.execute_received_code()
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

    def execute_received_code(self):
        """执行接收到的代码"""
        try:
            code = self.file_buffer.decode('utf-8')
            self.send_response("INFO", "代码开始执行...")
            _thread.start_new_thread(self._run_code_thread, (code,))
        except Exception as e:
            self.send_response("ERROR", f"代码执行失败: {str(e)}")
        finally:
            self.receiving_code = False
            self.file_buffer = bytearray()

    def run_code(self, command):
        """执行代码"""
        code = command.get('code')
        filename = command.get('filename')

        if self.code_running:
            self.send_response("ERROR", "已有代码在运行，请先发送stop命令")
            return

        try:
            if code:
                self.send_response("INFO", "代码开始执行...")
                _thread.start_new_thread(self._run_code_thread, (code, None))
            elif filename:
                if not self.file_exists(filename):
                    self.send_response("ERROR", f"文件不存在: {filename}")
                    return
                with open(filename, 'r') as f:
                    code = f.read()
                self.send_response("INFO", f"开始执行文件: {filename}")
                # 如果是临时文件，执行完后自动删除
                temp_file = filename if filename.startswith('_temp_') else None
                _thread.start_new_thread(self._run_code_thread, (code, temp_file))
            else:
                self.send_response("ERROR", "需要代码或文件名")
        except Exception as e:
            self.send_response("ERROR", f"执行失败: {str(e)}")

    def _run_code_thread(self, code, temp_file=None):
        """在线程中执行代码"""
        self.code_running = True
        self.stop_flag = False

        try:
            self.execute_code(code)
            if not self.stop_flag:
                self.send_response("SUCCESS", "代码执行完成", {"memory_free": gc.mem_free()})
        except Exception as e:
            if not self.stop_flag:
                self.send_response("ERROR", f"执行出错: {str(e)}")
        finally:
            self.code_running = False
            # 清理临时文件
            if temp_file:
                try:
                    os.remove(temp_file)
                except:
                    pass

    def stop_code(self):
        """停止正在运行的代码"""
        if self.code_running:
            self.stop_flag = True
            self.send_response("INFO", "正在停止代码...")
        else:
            self.send_response("INFO", "没有正在运行的代码")

    def execute_code(self, code):
        """执行代码字符串"""
        import machine
        import time

        def should_stop():
            return self.stop_flag

        # 自定义print函数，将输出发送到BLE
        def custom_print(*args, **kwargs):
            output = ' '.join(str(arg) for arg in args)
            print(output)  # 本地也打印
            self.send_response("OUTPUT", output)

        exec_globals = globals().copy()
        exec_globals.update({
            'print': custom_print,  # 使用自定义print
            'Pin': Pin,
            'Timer': Timer,
            'machine': machine,
            'time': time,
            'os': os,
            'gc': gc,
            'should_stop': should_stop
        })

        exec(code, exec_globals)

    def save_code(self, command):
        """保存代码到文件"""
        code = command.get('code')
        filename = command.get('filename')

        if not code or not filename:
            self.send_response("ERROR", "需要代码和文件名")
            return

        try:
            with open(filename, 'w') as f:
                f.write(code)
            self.send_response("SUCCESS", f"代码保存成功: {filename}", {
                "filename": filename,
                "size": len(code)
            })
        except Exception as e:
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
            "fs_total": self.get_fs_total(),
            "connection": "BLE"
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
