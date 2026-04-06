"""
ESP32 代码执行器 - 单文件版本
支持BLE和USB两种通信方式

注意：USB模式需要配合boot.py禁用REPL
"""

from machine import Pin, Timer
from time import sleep_ms
import time
import ubluetooth
import json
import os
import gc
import _thread
import sys
import select

# 配置参数
BLE_NAME_PREFIX = "M200-"
BLE_NAME_CONFIG_FILE = "_ble_name.txt"
MTU_SIZE = 512
USB_ENABLED = True

def _generate_default_ble_name():
    """生成默认名称：M200-随机6位数"""
    import random
    suffix = '{:06d}'.format(random.getrandbits(20) % 1000000)
    return BLE_NAME_PREFIX + suffix

def load_ble_name():
    """从配置文件加载 BLE 名称，不存在则生成随机默认名并持久化"""
    try:
        with open(BLE_NAME_CONFIG_FILE, 'r') as f:
            name = f.read().strip()
            if name:
                return name
    except:
        pass
    # 首次启动：生成随机名称并保存，确保同一设备名称固定
    default_name = _generate_default_ble_name()
    save_ble_name(default_name)
    return default_name

def save_ble_name(name):
    """将 BLE 名称保存到配置文件"""
    with open(BLE_NAME_CONFIG_FILE, 'w') as f:
        f.write(name)

BLE_DEVICE_NAME = load_ble_name()


class ESP32_BLE:
    def __init__(self, name):
        self.led = Pin(4, Pin.OUT)
        self.timer1 = Timer(0)
        self.name = name
        self.ble = ubluetooth.BLE()
        self.ble.active(True)
        sleep_ms(100)
        self.ble.config(gap_name=name, mtu=MTU_SIZE)
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
        self.need_cleanup = False
        self.code_running = False
        self.stop_flag = False
        # 线程安全响应队列：子线程将响应放入此列表，主循环统一发送
        self._resp_queue = []

        print("ESP32代码执行器已启动")
        print("可用命令: run, save, list, delete, reboot, info, stop")

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
            self.need_advertise = True
            self.need_cleanup = True
            self.disconnected()

        elif event == 3:  # _IRQ_GATTS_WRITE
            conn_handle, attr_handle = data

            if attr_handle == self.rx:
                buffer = self.ble.gatts_read(self.rx)
                print(f"[IRQ] RX写入, 长度: {len(buffer)}")
                self.process_command(buffer)

            elif attr_handle == self.code:
                buffer = self.ble.gatts_read(self.code)
                print(f"[IRQ] CODE写入, 块大小: {len(buffer)}")
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
            print("服务注册返回值：", handles)

            self.rx = handles[0][0]
            self.tx = handles[0][1]
            self.code = handles[0][2]
            print(f"RX句柄: {self.rx}, TX句柄: {self.tx}, CODE句柄: {self.code}")

            self.ble.gatts_set_buffer(self.rx, 512, True)
            self.ble.gatts_set_buffer(self.code, 1024, True)

        except Exception as e:
            print("服务注册失败：", e)
            raise

    def send_response(self, status, message="", data=None):
        """发送JSON响应到客户端（线程安全）
        子线程（code_running 期间）将响应入队，由主循环统一发送；
        主线程/IRQ 直接发送。"""
        if not self.is_connected:
            print(f"[BLE-SKIP] {status}: {message}")
            return

        response = {
            "status": status,
            "message": message,
            "data": data
        }

        # 子线程中不直接操作 BLE，放入队列
        if self.code_running:
            self._resp_queue.append(response)
            return

        self._do_send(response)

    def _do_send(self, response):
        """实际 BLE notify 发送（只在主线程调用）"""
        try:
            response_str = json.dumps(response)
            resp_bytes = response_str.encode('utf-8')
            print(f"[BLE-RESP] {response['status']} ({len(resp_bytes)}字节): {response_str[:80]}")
            # 分块 notify，避免超出 MTU 被截断
            chunk_size = 80
            for i in range(0, len(resp_bytes), chunk_size):
                self.ble.gatts_notify(self.conn_handle, self.tx, resp_bytes[i:i+chunk_size])
                if i + chunk_size < len(resp_bytes):
                    sleep_ms(20)
        except Exception as e:
            print(f"[BLE-RESP] 发送失败: {e}")

    def flush_responses(self):
        """主循环调用：发送队列中积累的响应"""
        while self._resp_queue and self.is_connected:
            resp = self._resp_queue.pop(0)
            self._do_send(resp)
            sleep_ms(10)

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
            elif cmd_type == 'start_run':
                self.start_code_run(command)
            elif cmd_type == 'stop':
                self.stop_code()
            elif cmd_type == 'remote_joystick':
                self.remote_joystick(command)
            elif cmd_type == 'remote_stop':
                self.remote_stop()
            elif cmd_type == 'remote_rgb':
                self.remote_rgb(command)
            elif cmd_type == 'remote_skill':
                self.remote_skill(command)
            elif cmd_type == 'burn':
                import _autorun
                _autorun.handle_burn(command, self.send_response, self.code_running, self._run_code_thread)
            elif cmd_type == 'clear_autorun':
                import _autorun
                _autorun.handle_clear(self.send_response)
            elif cmd_type == 'set_name':
                self.set_device_name(command)
            elif cmd_type == 'get_name':
                self.send_response("SUCCESS", self.name)
            else:
                self.send_response("ERROR", f"未知命令: {cmd_type}")

        except Exception as e:
            self.send_response("ERROR", f"命令处理失败: {str(e)}")


    # ---- 设备名称修改 ----

    def set_device_name(self, command):
        """修改 BLE 设备名称，保存到文件并重启 BLE 广播"""
        new_name = command.get('name', '').strip()
        if not new_name:
            self.send_response("ERROR", "名称不能为空")
            return
        # 强制 M200- 前缀
        if not new_name.startswith(BLE_NAME_PREFIX):
            new_name = BLE_NAME_PREFIX + new_name
        # BLE 广播名称最长约 29 字节
        name_bytes = new_name.encode('utf-8')
        if len(name_bytes) > 29:
            self.send_response("ERROR", "名称过长（UTF-8 不超过 29 字节）")
            return
        try:
            save_ble_name(new_name)
            self.name = new_name
            self.ble.config(gap_name=new_name)
            # 先回复成功，再断连让客户端重新扫描
            self.send_response("SUCCESS", f"名称已修改为: {new_name}")
            sleep_ms(300)
            # 断开当前连接，重新广播新名称
            if self.conn_handle is not None:
                try:
                    self.ble.gap_disconnect(self.conn_handle)
                except:
                    pass
            self.need_advertise = True
        except Exception as e:
            self.send_response("ERROR", f"修改名称失败: {str(e)}")

    # ---- 遥控命令处理 ----

    def remote_joystick(self, command):
        """处理遥控摇杆命令"""
        x = command.get('x', 0)
        y = command.get('y', 0)
        speed = command.get('speed', 0)
        direction = command.get('dir', 'center')
        print(f"遥控摇杆: dir={direction}, x={x}, y={y}, speed={speed}")
        # TODO: 接入电机控制逻辑
        self.send_response("SUCCESS", f"joystick: {direction}")

    def remote_stop(self):
        """处理遥控停止命令"""
        print("遥控停止")
        # TODO: 停止电机
        self.send_response("SUCCESS", "remote stopped")

    def remote_rgb(self, command):
        """处理遥控RGB命令"""
        hue = command.get('hue', 0)
        print(f"遥控RGB: hue={hue}")
        # TODO: 接入LED/灯光控制逻辑
        self.send_response("SUCCESS", f"rgb: hue={hue}")

    def remote_skill(self, command):
        """处理遥控技能命令"""
        skill = command.get('skill', '')
        print(f"遥控技能: {skill}")
        # TODO: 接入技能动作逻辑
        self.send_response("SUCCESS", f"skill: {skill}")

    def start_file_upload(self, command):
        """开始文件上传"""
        filename = command.get('filename')
        total_size = command.get('size', 0)
        print(f"开始上传 - 文件名: {filename}, 大小: {total_size}")

        if not filename:
            self.send_response("ERROR", "需要文件名")
            return

        self.receiving_file = True
        self.receiving_code = False
        self.current_filename = filename
        self.file_buffer = bytearray()
        self.expected_size = total_size

        print("发送 READY 响应...")
        self.send_response("READY", f"准备接收文件: {filename}", {
            "filename": filename,
            "buffer_size": 0
        })
        print("READY 响应已发送")

    def start_code_run(self, command):
        """开始接收代码执行（分块传输）"""
        total_size = command.get('size', 0)
        print(f"开始接收代码执行 - 大小: {total_size}")

        if self.code_running:
            self.send_response("ERROR", "已有代码在运行，请先发送stop命令")
            return

        self.receiving_file = False
        self.receiving_code = True
        self.file_buffer = bytearray()
        self.expected_size = total_size

        print("发送 READY_RUN 响应...")
        self.send_response("READY_RUN", "准备接收代码", {
            "buffer_size": 0
        })
        print("READY_RUN 响应已发送")

    def process_code_data(self, data):
        """处理代码数据块"""
        if not self.receiving_file and not self.receiving_code:
            print("[DATA] 错误: 收到数据但未处于接收状态")
            self.send_response("ERROR", "未处于接收状态")
            return

        self.file_buffer.extend(data)
        progress = (len(self.file_buffer) / self.expected_size * 100) if self.expected_size > 0 else 0
        print(f"[DATA] +{len(data)}字节, 已收: {len(self.file_buffer)}/{self.expected_size} ({progress:.1f}%)")

        if len(self.file_buffer) >= self.expected_size:
            print(f"[DATA] 接收完成! 总计: {len(self.file_buffer)}字节")
            if self.receiving_file:
                self.save_complete_file()
            elif self.receiving_code:
                self.execute_received_code()
        else:
            # 每25%发送一次进度，避免notify拥塞导致传输卡住
            if int(progress) % 25 == 0 or progress < 5:
                self.send_response("PROGRESS", "接收中...", {
                    "received": len(self.file_buffer),
                    "total": self.expected_size,
                    "progress": progress
                })

    def save_complete_file(self):
        """保存完整的文件"""
        print(f"[BLE-SAVE] 开始保存文件: {self.current_filename}, 大小: {len(self.file_buffer)}")
        try:
            with open(self.current_filename, 'wb') as f:
                f.write(self.file_buffer)

            print(f"[BLE-SAVE] 文件保存成功: {self.current_filename}")
            self.send_response("SUCCESS", f"文件保存成功: {self.current_filename}", {
                "filename": self.current_filename,
                "size": len(self.file_buffer)
            })
        except Exception as e:
            print(f"[BLE-SAVE] 文件保存失败: {e}")
            self.send_response("ERROR", f"文件保存失败: {str(e)}")
        finally:
            self.receiving_file = False
            self.current_filename = None
            self.file_buffer = bytearray()

    def execute_received_code(self):
        """执行接收到的代码"""
        print(f"[BLE-EXEC] 准备执行代码, buffer大小: {len(self.file_buffer)}")
        try:
            code = self.file_buffer.decode('utf-8')
            print(f"[BLE-EXEC] 代码解码成功, 长度: {len(code)}")
            print(f"[BLE-EXEC] 代码前100字符: {code[:100]}")

            self.send_response("INFO", "代码开始执行...")
            _thread.start_new_thread(self._run_code_thread, (code,))
        except Exception as e:
            print(f"[BLE-EXEC] 代码执行失败: {e}")
            self.send_response("ERROR", f"代码执行失败: {str(e)}")
        finally:
            self.receiving_code = False
            self.file_buffer = bytearray()

    def run_code(self, command):
        """执行代码"""
        code = command.get('code')
        filename = command.get('filename')
        print(f"执行代码 - 代码长度: {len(code) if code else 0}, 文件名: {filename}")

        if self.code_running:
            self.send_response("ERROR", "已有代码在运行，请先发送stop命令")
            return

        try:
            if code:
                if 'while True' in code or 'while 1' in code:
                    print("检测到循环，将在线程中运行")
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
            print(f"执行异常: {e}")
            self.send_response("ERROR", f"执行失败: {str(e)}")

    def _run_code_thread(self, code, temp_file=None):
        """在线程中执行代码"""
        self.code_running = True
        self.stop_flag = False

        try:
            self.execute_code(code)
            if self.stop_flag:
                self.send_response("SUCCESS", "代码已停止")
            else:
                self.send_response("SUCCESS", "代码执行完成", {
                    "memory_free": gc.mem_free()
                })
        except Exception as e:
            if self.stop_flag:
                self.send_response("SUCCESS", "代码已停止")
            else:
                self.send_response("ERROR", f"执行出错: {str(e)}")
        finally:
            self.code_running = False
            # 清理临时文件
            if temp_file:
                try:
                    os.remove(temp_file)
                except:
                    pass
            print("线程执行结束")

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

        # 自定义print函数，将输出发送回客户端
        def custom_print(*args, **kwargs):
            output = ' '.join(str(arg) for arg in args)
            print(output)  # 本地也打印
            self.send_response("OUTPUT", output)

        # 将 while True 替换为可中断循环
        code = code.replace('while True:', 'while not should_stop():')
        code = code.replace('while 1:', 'while not should_stop():')

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


class ESP32_USB:
    """
    USB串口通信类

    注意：需要配合boot.py禁用REPL才能正常工作
    """
    def __init__(self):
        self.led = Pin(4, Pin.OUT)
        self.poll = select.poll()
        self.poll.register(sys.stdin, select.POLLIN)

        # 文件传输状态
        self.receiving_file = False
        self.receiving_code = False
        self.current_filename = None
        self.file_buffer = bytearray()
        self.expected_size = 0
        self.code_running = False
        self.stop_flag = False

        # 接收缓冲区
        self.rx_buffer = ""
        self.last_rx_time = time.ticks_ms()

        print("USB串口通信已启动")

    def send_response(self, status, message="", data=None):
        """发送JSON响应到客户端"""
        response = {"status": status, "message": message, "data": data}

        try:
            response_str = json.dumps(response)
            print(f"USB_RESP:{response_str}")
        except Exception:
            pass

    def check_input(self):
        """非阻塞检查串口输入"""
        # 超时清理：如果缓冲区有数据但超过500ms没有新数据，清空它
        if self.rx_buffer and time.ticks_diff(time.ticks_ms(), self.last_rx_time) > 500:
            self.rx_buffer = ""

        # 每次最多读100个字符，避免长时间阻塞主循环
        for _ in range(100):
            result = self.poll.poll(0)
            if not result:
                break
            try:
                char = sys.stdin.read(1)
                if not char:
                    break
                self.last_rx_time = time.ticks_ms()
                if char == '\n' or char == '\r':
                    if self.rx_buffer:
                        self.process_input(self.rx_buffer)
                        self.rx_buffer = ""
                else:
                    self.rx_buffer += char
            except Exception:
                break

    def process_input(self, data):
        """处理输入数据"""
        data = data.strip()
        if not data:
            return

        if self.receiving_file or self.receiving_code:
            self.process_data_chunk(data)
            return

        if data.startswith('{'):
            self.process_command(data)
        else:
            self.process_simple_command(data)

    def process_simple_command(self, cmd):
        """处理简单文本命令"""
        parts = cmd.split(' ', 1)
        cmd_type = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else None

        if cmd_type == 'list':
            self.list_files()
        elif cmd_type == 'info':
            self.get_system_info()
        elif cmd_type == 'reboot':
            self.reboot_esp()
        elif cmd_type == 'stop':
            self.stop_code()
        elif cmd_type == 'delete' and arg:
            self.delete_file({'filename': arg})
        else:
            self.send_response("ERROR", f"未知命令: {cmd}")

    def process_command(self, data):
        """处理JSON命令"""
        try:
            command = json.loads(data)
            cmd_type = command.get('type')

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
            elif cmd_type == 'remote_joystick':
                self.remote_joystick(command)
            elif cmd_type == 'remote_stop':
                self.remote_stop()
            elif cmd_type == 'remote_rgb':
                self.remote_rgb(command)
            elif cmd_type == 'remote_skill':
                self.remote_skill(command)
            elif cmd_type == 'burn':
                import _autorun
                _autorun.handle_burn(command, self.send_response, self.code_running, self._run_code_thread)
            elif cmd_type == 'clear_autorun':
                import _autorun
                _autorun.handle_clear(self.send_response)
            elif cmd_type == 'set_name':
                self.set_device_name(command)
            elif cmd_type == 'get_name':
                self.send_response("SUCCESS", load_ble_name())
            else:
                self.send_response("ERROR", f"未知命令: {cmd_type}")

        except Exception as e:
            self.send_response("ERROR", f"命令处理失败: {str(e)}")


    # ---- 设备名称修改（USB 侧） ----

    def set_device_name(self, command):
        """通过 USB 修改 BLE 设备名称（保存到文件，下次启动生效）"""
        new_name = command.get('name', '').strip()
        if not new_name:
            self.send_response("ERROR", "名称不能为空")
            return
        if not new_name.startswith(BLE_NAME_PREFIX):
            new_name = BLE_NAME_PREFIX + new_name
        name_bytes = new_name.encode('utf-8')
        if len(name_bytes) > 29:
            self.send_response("ERROR", "名称过长（UTF-8 不超过 29 字节）")
            return
        try:
            save_ble_name(new_name)
            self.send_response("SUCCESS", f"名称已保存为: {new_name}（重启后生效）")
        except Exception as e:
            self.send_response("ERROR", f"修改名称失败: {str(e)}")

    # ---- 遥控命令处理 ----

    def remote_joystick(self, command):
        """处理遥控摇杆命令"""
        x = command.get('x', 0)
        y = command.get('y', 0)
        speed = command.get('speed', 0)
        direction = command.get('dir', 'center')
        print(f"遥控摇杆: dir={direction}, x={x}, y={y}, speed={speed}")
        # TODO: 接入电机控制逻辑
        self.send_response("SUCCESS", f"joystick: {direction}")

    def remote_stop(self):
        """处理遥控停止命令"""
        print("遥控停止")
        # TODO: 停止电机
        self.send_response("SUCCESS", "remote stopped")

    def remote_rgb(self, command):
        """处理遥控RGB命令"""
        hue = command.get('hue', 0)
        print(f"遥控RGB: hue={hue}")
        # TODO: 接入LED/灯光控制逻辑
        self.send_response("SUCCESS", f"rgb: hue={hue}")

    def remote_skill(self, command):
        """处理遥控技能命令"""
        skill = command.get('skill', '')
        print(f"遥控技能: {skill}")
        # TODO: 接入技能动作逻辑
        self.send_response("SUCCESS", f"skill: {skill}")

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
        """开始接收代码执行"""
        total_size = command.get('size', 0)

        if self.code_running:
            self.send_response("ERROR", "已有代码在运行，请先发送stop命令")
            return

        self.receiving_file = False
        self.receiving_code = True
        self.file_buffer = bytearray()
        self.expected_size = total_size

        self.send_response("READY_RUN", "准备接收代码", {"buffer_size": 0})

    def process_data_chunk(self, data):
        """处理数据块（Base64编码）"""
        import ubinascii
        try:
            if data.startswith('DATA:'):
                b64_data = data[5:]
                # Base64 padding 容错：补全到4的倍数
                padding = len(b64_data) % 4
                if padding:
                    b64_data += '=' * (4 - padding)

                chunk = ubinascii.a2b_base64(b64_data)
                self.file_buffer.extend(chunk)

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
            elif data == 'END':
                if self.receiving_file:
                    self.save_complete_file()
                elif self.receiving_code:
                    self.execute_received_code()
        except Exception as e:
            self.send_response("ERROR", f"数据处理失败: {str(e)}")
            self.receiving_file = False
            self.receiving_code = False
            self.file_buffer = bytearray()

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
            if self.stop_flag:
                self.send_response("SUCCESS", "代码已停止")
            else:
                self.send_response("SUCCESS", "代码执行完成", {"memory_free": gc.mem_free()})
        except Exception as e:
            if self.stop_flag:
                self.send_response("SUCCESS", "代码已停止")
            else:
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

        # 自定义print函数，将输出发送回客户端
        def custom_print(*args, **kwargs):
            output = ' '.join(str(arg) for arg in args)
            print(output)  # 本地也打印
            self.send_response("OUTPUT", output)

        # 将 while True 替换为可中断循环
        code = code.replace('while True:', 'while not should_stop():')
        code = code.replace('while 1:', 'while not should_stop():')

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
            "connection": "USB"
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


def main():
    global button_flag
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
