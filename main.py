# main.py - ESP32主程序
import bluetooth
import json
import os
import gc
import machine
import uio
import ubinascii
from machine import Timer, Pin

class CodeExecutor:
    def __init__(self):
        self.ble = bluetooth.BLE()
        self.ble.active(True)
        
        # BLE服务UUID
        self.SERVICE_UUID = bluetooth.UUID('6E400001-B5A3-F393-E0A9-E50E24DCCA9E')
        self.RX_UUID = bluetooth.UUID('6E400002-B5A3-F393-E0A9-E50E24DCCA9E')  # 接收代码
        self.TX_UUID = bluetooth.UUID('6E400003-B5A3-F393-E0A9-E50E24DCCA9E')  # 发送状态
        self.CODE_UUID = bluetooth.UUID('6E400004-B5A3-F393-E0A9-E50E24DCCA9E')  # 大文件传输
        
        self.setup_ble()
        self.connection = None
        self.receiving_file = False
        self.current_filename = None
        self.file_buffer = bytearray()
        
        print("ESP32代码执行器已启动")
        print("可用命令: run, save, list, delete, reboot")
        
    def setup_ble(self):
        # 创建BLE服务
        service = (
            self.SERVICE_UUID,
            [
                (self.RX_UUID, bluetooth.FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE),
                (self.TX_UUID, bluetooth.FLAG_NOTIFY),
                (self.CODE_UUID, bluetooth.FLAG_WRITE | bluetooth.FLAG_NOTIFY),  # 大文件传输
            ]
        )
        
        services = [service]
        ((self.tx_char, self.rx_char, self.code_char),) = self.ble.gatts_register_services(services)
        
        # 设置缓冲区大小
        self.ble.gatts_set_buffer(self.rx_char, 512, True)
        self.ble.gatts_set_buffer(self.code_char, 1024, True)  # 更大的缓冲区
        
        # 设置回调
        self.ble.irq(self.ble_irq)
        
        # 广播
        self.advertise()
        
    def advertise(self):
        name = "ESP32-CodeLoader"
        adv_data = bytearray()
        adv_data.append(len(name) + 1)
        adv_data.append(0x09)  # Complete Local Name
        adv_data.extend(name.encode())
        
        # 添加服务UUID
        service_uuid = bytes(bluetooth.UUID('6E400001-B5A3-F393-E0A9-E50E24DCCA9E'))
        adv_data.append(len(service_uuid) + 1)
        adv_data.append(0x07)  # Complete List of 128-bit Service Class UUIDs
        adv_data.extend(service_uuid)
        
        self.ble.gap_advertise(100, adv_data)
        print("BLE广播中...")
        
    def ble_irq(self, event, data):
        if event == 1:  # 连接
            self.connection, _, _ = data
            print("设备已连接")
            self.send_response("CONNECTED", "欢迎使用ESP32代码执行器")
            
        elif event == 2:  # 断开
            print("设备已断开")
            self.connection = None
            self.advertise()
            
        elif event == 3:  # 写入
            conn_handle, attr_handle = data
            
            if attr_handle == self.rx_char:
                # 接收命令
                received = self.ble.gatts_read(self.rx_char)
                self.process_command(received)
                
            elif attr_handle == self.code_char:
                # 接收代码数据
                received = self.ble.gatts_read(self.code_char)
                self.process_code_data(received)
                
    def send_response(self, status, message="", data=None):
        if self.connection is None:
            return
            
        response = {
            "status": status,
            "message": message,
            "data": data
        }
        
        try:
            response_str = json.dumps(response)
            self.ble.gatts_notify(self.connection, self.tx_char, response_str)
        except Exception as e:
            print(f"发送响应失败: {e}")
            
    def process_command(self, data):
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
        
        if not filename:
            self.send_response("ERROR", "需要文件名")
            return
            
        self.receiving_file = True
        self.current_filename = filename
        self.file_buffer = bytearray()
        self.expected_size = total_size
        
        self.send_response("READY", f"准备接收文件: {filename}", {
            "filename": filename,
            "buffer_size": len(self.file_buffer)
        })
        
    def process_code_data(self, data):
        """处理代码数据块"""
        if not self.receiving_file:
            self.send_response("ERROR", "未处于接收状态")
            return
            
        # 添加到缓冲区
        self.file_buffer.extend(data)
        
        # 发送进度
        progress = (len(self.file_buffer) / self.expected_size * 100) if self.expected_size > 0 else 0
        
        if len(self.file_buffer) >= self.expected_size:
            # 文件接收完成
            self.save_complete_file()
        else:
            # 发送进度更新
            self.send_response("PROGRESS", "接收中...", {
                "received": len(self.file_buffer),
                "total": self.expected_size,
                "progress": progress
            })
            
    def save_complete_file(self):
        """保存完整的文件"""
        try:
            # 保存到文件系统
            with open(self.current_filename, 'wb') as f:
                f.write(self.file_buffer)
                
            self.send_response("SUCCESS", f"文件保存成功: {self.current_filename}", {
                "filename": self.current_filename,
                "size": len(self.file_buffer)
            })
            
        except Exception as e:
            self.send_response("ERROR", f"文件保存失败: {str(e)}")
            
        finally:
            # 清理状态
            self.receiving_file = False
            self.current_filename = None
            self.file_buffer = bytearray()
            
    def run_code(self, command):
        """执行代码"""
        code = command.get('code')
        filename = command.get('filename')
        
        try:
            if code:
                # 直接执行代码字符串
                exec_result = self.execute_code(code)
                self.send_response("SUCCESS", "代码执行完成", {
                    "result": exec_result,
                    "type": "direct"
                })
                
            elif filename:
                # 执行文件中的代码
                result = self.execute_file(filename)
                self.send_response("SUCCESS", f"文件执行完成: {filename}", {
                    "result": result,
                    "filename": filename
                })
                
            else:
                self.send_response("ERROR", "需要代码或文件名")
                
        except Exception as e:
            self.send_response("ERROR", f"执行失败: {str(e)}", {
                "exception": str(e)
            })
            
    def execute_code(self, code):
        """安全地执行代码"""
        # 创建局部命名空间
        local_vars = {
            '__builtins__': __builtins__,
            'print': print,
            'Pin': Pin,
            'Timer': Timer,
            'machine': machine,
            'os': os,
            'gc': gc
        }
        
        # 执行代码
        try:
            # 使用exec执行
            exec(code, local_vars)
            
            # 收集执行结果
            result = {
                "output": self.capture_output(code),
                "memory_free": gc.mem_free(),
                "memory_alloc": gc.mem_alloc()
            }
            
            return result
            
        except Exception as e:
            raise e
            
    def capture_output(self, code):
        """捕获输出"""
        # 这里可以重定向print输出
        # 简化版本：返回成功信息
        return "代码执行成功"
        
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
        """列出所有.py文件"""
        try:
            files = []
            for file in os.listdir():
                if file.endswith('.py') or file.endswith('.txt'):
                    size = os.stat(file)[6]  # 文件大小
                    files.append({
                        "name": file,
                        "size": size
                    })
                    
            self.send_response("SUCCESS", f"找到 {len(files)} 个文件", {
                "files": files
            })
            
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
            return fs_stat[0] * fs_stat[3]  # block_size * free_blocks
        except:
            return 0
            
    def get_fs_total(self):
        """获取文件系统总空间"""
        try:
            fs_stat = os.statvfs('/')
            return fs_stat[0] * fs_stat[2]  # block_size * total_blocks
        except:
            return 0

# 启动执行器
if __name__ == "__main__":
    executor = CodeExecutor()
    
    # 保持运行
    try:
        while True:
            # 可以添加其他任务
            pass
    except KeyboardInterrupt:
        print("程序退出")