# ESP32 无线编程器

通过蓝牙低功耗(BLE)或USB串口向ESP32发送并执行MicroPython代码的无线编程工具。

## 功能特性

- **双通信模式**: 支持蓝牙(BLE)和USB串口两种连接方式，可根据场景灵活选择
- **在线代码执行**: 无需重新烧录固件，直接在线执行MicroPython代码
- **文件管理**: 支持保存、列出、删除ESP32文件系统中的文件
- **大文件传输**: 支持分块传输大文件，突破BLE MTU限制
- **代码停止控制**: 支持运行时停止正在执行的代码（使用`should_stop()`函数）
- **系统监控**: 实时查看ESP32内存和文件系统使用情况

## 系统组成

### 1. ESP32固件 (`xiao.py`)

运行在ESP32上的MicroPython固件，提供：

- BLE GATT服务，设备名"ESP32-CodeLoader"
- USB串口通信支持
- 命令解析与代码执行
- 文件系统操作

### 2. Web IDE (`index.html`)

基于浏览器的开发环境，提供：

- 代码编辑器
- 蓝牙/USB连接管理
- 文件操作界面
- 执行日志显示
- 示例代码模板

## 快速开始

### 准备工作

1. ESP32开发板（已烧录MicroPython固件）
2. 支持Web Bluetooth/Web Serial的浏览器（Chrome、Edge等）

### 安装步骤

1. 将固件文件上传到ESP32：
   ```bash
   # 使用mpremote工具
   mpremote cp main.py :main.py
   mpremote cp ble.py :ble.py
   mpremote cp usb.py :usb.py

   # 或使用ampy
   ampy --port /dev/ttyUSB0 put main.py
   ampy --port /dev/ttyUSB0 put ble.py
   ampy --port /dev/ttyUSB0 put usb.py
   ```

2. 重启ESP32，固件会自动启动

3. 打开`index.html`（需要HTTPS或localhost环境）

### USB通信说明

USB模式使用ESP32原生USB串口，需要禁用REPL才能正常工作。

### 启用USB模式

上传`boot.py`文件到ESP32：

```bash
mpremote cp boot.py :boot.py
```

`boot.py`会在启动时禁用REPL，使USB串口可被程序使用。

### 恢复REPL调试

如需恢复REPL调试功能，删除ESP32上的`boot.py`文件：

```bash
mpremote rm :boot.py
```

或通过Web界面的"删除文件"功能删除。

## 连接设备

**蓝牙连接：**
1. 选择"蓝牙 (BLE)"
2. 点击"扫描并连接设备"
3. 选择"ESP32-CodeLoader"设备

**USB连接：**
1. 通过USB线连接ESP32
2. 选择"USB 串口"
3. 点击"扫描并连接设备"
4. 选择对应的COM端口

## 通信协议

### BLE协议

使用Nordic UART Service (NUS)：

| 特征 | UUID | 功能 |
|------|------|------|
| Service | `6E400001-B5A3-F393-E0A9-E50E24DCCA9E` | NUS服务 |
| RX | `6E400002-...` | 接收命令 |
| TX | `6E400003-...` | 发送响应 |
| CODE | `6E400004-...` | 大文件传输 |

### USB协议

- 波特率：115200
- 命令格式：JSON字符串 + 换行符
- 响应格式：`USB_RESP:` + JSON字符串

### 命令格式

所有命令均为JSON格式：

```json
// 执行代码
{"type": "run", "code": "print('Hello')"}

// 执行文件
{"type": "run", "filename": "main.py"}

// 保存文件
{"type": "save", "filename": "test.py", "code": "..."}

// 开始大文件上传
{"type": "start_upload", "filename": "app.py", "size": 1024}

// 开始代码执行（大代码块）
{"type": "start_run", "size": 2048}

// 列出文件
{"type": "list"}

// 删除文件
{"type": "delete", "filename": "test.py"}

// 系统信息
{"type": "info"}

// 停止执行
{"type": "stop"}

// 重启
{"type": "reboot"}
```

### 响应格式

```json
{
  "status": "SUCCESS|ERROR|INFO|READY|PROGRESS",
  "message": "描述信息",
  "data": { ... }
}
```

## 硬件配置

| 功能 | GPIO | 说明 |
|------|------|------|
| 状态LED | 4 | 蓝牙连接状态指示 |
| 辅助LED | 15 | 用户自定义 |
| 按键 | 26 | 上拉输入，下降沿中断 |

## 编写可停止的代码

使用`should_stop()`函数实现可停止的循环：

```python
from machine import Pin
import time

led = Pin(2, Pin.OUT)

# 使用 should_stop() 检查是否需要停止
while not should_stop():
    led.value(not led.value())
    time.sleep(0.5)

led.value(0)
print("已停止")
```

## 浏览器兼容性

| 功能 | Chrome | Edge | Firefox | Safari |
|------|--------|------|---------|--------|
| Web Bluetooth (BLE) | ✅ | ✅ | ❌ | ❌ |
| Web Serial (USB) | ✅ | ✅ | ❌ | ❌ |

> 注意：Web Bluetooth和Web Serial需要HTTPS环境或localhost

## 故障排除

### BLE连接问题

1. 确保ESP32蓝牙已启动（LED闪烁表示等待连接）
2. 确保浏览器支持Web Bluetooth
3. 检查是否在HTTPS环境下运行

### USB连接问题

1. 确保已安装USB驱动
2. 检查COM端口是否被占用
3. 确认波特率设置为115200

### 代码执行问题

1. 检查代码语法是否正确
2. 确保没有其他代码正在运行
3. 使用`should_stop()`使循环可中断

## 项目结构

提供两种部署方案：

### 方案A：单文件版本（简单部署）
```
esp32-demo/
├── xiao.py      # 单文件完整版（BLE+USB）
└── index.html   # Web IDE界面
```

### 方案B：模块化版本（易维护）
```
esp32-demo/
├── main.py      # 主程序入口
├── ble.py       # BLE通信模块
├── usb.py       # USB串口通信模块
└── index.html   # Web IDE界面
```

## 部署说明

### 方案A：单文件版本

```bash
# 使用mpremote
mpremote cp xiao.py :main.py

# 或使用ampy
ampy --port /dev/ttyUSB0 put xiao.py main.py
```

### 方案B：模块化版本

```bash
# 使用mpremote
mpremote cp main.py :main.py
mpremote cp ble.py :ble.py
mpremote cp usb.py :usb.py

# 或使用ampy
ampy --port /dev/ttyUSB0 put main.py
ampy --port /dev/ttyUSB0 put ble.py
ampy --port /dev/ttyUSB0 put usb.py
```

前端


```bash
python -m http.server 8000
```


如果只需要BLE或USB功能，可以只上传对应模块，并修改`main.py`中的配置：

```python
BLE_ENABLED = True   # 是否启用BLE
USB_ENABLED = True   # 是否启用USB
```

## License

MIT License
