"""
ESP32 USB串口通信模块
通过原生USB串口接收命令和发送响应

注意：需要配合boot.py禁用REPL才能正常工作
"""

from machine import Pin, Timer
import json
import os
import gc
import _thread
import sys
import select


import time


class ESP32_USB:
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
        import machine
        import time

        def should_stop():
            return self.stop_flag

        def custom_print(*args):
            self.send_response("OUTPUT", " ".join(str(a) for a in args))

        exec_globals = {
            'print': custom_print,
            'Pin': Pin,
            'Timer': Timer,
            'time': time,
            'machine': machine,
            'gc': gc,
            'should_stop': should_stop,
        }

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
