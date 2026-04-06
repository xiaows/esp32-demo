"""
ESP32 烧录脱机运行模块
- 管理 _autorun.cfg 自启动配置
- 提供 burn / clear_autorun 命令处理
"""
import os
import _thread

CONFIG_FILE = "_autorun.cfg"
CODE_FILE = "_user_code.py"


def load_config():
    """读取自启动配置，返回文件名或 None"""
    try:
        with open(CONFIG_FILE, 'r') as f:
            name = f.read().strip()
            return name if name else None
    except:
        return None


def save_config(filename):
    """保存自启动配置"""
    with open(CONFIG_FILE, 'w') as f:
        f.write(filename)


def clear_config():
    """清除自启动配置"""
    try:
        os.remove(CONFIG_FILE)
    except:
        pass


def file_exists(filename):
    try:
        os.stat(filename)
        return True
    except OSError:
        return False


def handle_burn(command, send_response, code_running, run_code_thread):
    """处理 burn 命令：设置自启动 + 立即运行"""
    filename = command.get('filename', CODE_FILE)
    if not file_exists(filename):
        send_response("ERROR", f"文件不存在: {filename}")
        return
    try:
        save_config(filename)
        send_response("SUCCESS", "烧录成功，开始执行...", {
            "filename": filename,
            "autorun": True
        })
        if not code_running:
            with open(filename, 'r') as f:
                code = f.read()
            _thread.start_new_thread(run_code_thread, (code, None))
    except Exception as e:
        send_response("ERROR", f"烧录失败: {str(e)}")


def handle_clear(send_response):
    """处理 clear_autorun 命令"""
    clear_config()
    send_response("SUCCESS", "已清除自启动")


def check_and_run(run_code_thread):
    """开机检查自启动，有则在线程中执行"""
    autorun_file = load_config()
    if autorun_file and file_exists(autorun_file):
        print(f"[AUTORUN] 发现自启动脚本: {autorun_file}")
        try:
            with open(autorun_file, 'r') as f:
                code = f.read()
            _thread.start_new_thread(run_code_thread, (code, None))
        except Exception as e:
            print(f"[AUTORUN] 启动失败: {e}")
