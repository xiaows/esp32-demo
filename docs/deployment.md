# 启动与部署说明

本文档用于说明这个项目在 ESP32 上的启动方式、文件职责，以及开发调试与工厂出厂两种部署思路。

## 1. 先说结论

- 这是一个 **MicroPython + 浏览器前端** 项目，不是传统的“编译源码生成固件”项目。
- 项目里的 `main.py`、`ble.py`、`usb.py`、`xiao.py`、`_autorun.py`、`boot.py`、`Servo.py` 都是脚本文件，通常直接上传到 ESP32 文件系统运行。
- 前端 `index.html` 也不需要构建，使用本地 HTTP 服务或部署到网页服务器即可。
- 对 ESP32 而言，真正的应用启动入口始终是设备上的 `main.py`。

## 2. ESP32 启动顺序

更准确地说，是 MicroPython 在“上电或复位后”的执行顺序如下：

```text
上电 / 复位
  ↓
boot.py（如果存在）
  ↓
main.py
  ↓
初始化 BLE / USB / 按键 / LED
  ↓
检查 _autorun 配置
  ↓
进入主循环
```

说明：

- `boot.py`
  - 属于启动前置配置文件。
  - 当前项目里主要用于禁用 REPL，让 USB 串口可以被程序接管。
- `main.py`
  - 是真正的业务入口。
  - 模块化方案下，设备上直接放 `main.py`。
  - 单文件方案下，通常是把 `xiao.py` 上传并重命名成设备上的 `main.py`。

## 3. 主要文件职责

### 3.1 设备端

- `main.py`
  - 模块化方案的主入口。
  - 负责初始化 BLE、USB、按键、LED，并进入主循环。
- `ble.py`
  - BLE 通信模块。
  - 负责 BLE 广播、命令解析、文件传输、代码执行等。
- `usb.py`
  - USB 串口通信模块。
  - 负责串口命令解析、文件传输、代码执行等。
- `xiao.py`
  - 单文件完整版。
  - 内部同时包含 BLE 和 USB 逻辑，适合简化部署。
- `_autorun.py`
  - 自启动辅助模块。
  - 负责保存“烧录后自动运行”的配置，并在开机时触发执行。
- `boot.py`
  - 可选启动配置。
  - 仅在需要 USB 模式时建议上传。
- `Servo.py`
  - 扩展模块。
  - 主要给示例代码或外设控制使用，不是基础启动必需文件。

### 3.2 前端

- `index.html`
  - 浏览器端 Web IDE。
  - 通过 Web Serial 或 Web Bluetooth 与 ESP32 通信。
  - 不需要编译，直接托管即可。

## 4. 这些文件是否需要编译

不需要传统意义上的编译。

### 4.1 设备端

- 设备端脚本直接上传到 ESP32 文件系统。
- 运行依赖是板子上预先烧录好的 MicroPython 固件。
- 所以真正需要先刷到板子里的“固件”，是 MicroPython 本身，而不是这个仓库里的这些 `.py` 文件。

### 4.2 前端

- `index.html` 是静态文件。
- 运行时只需要浏览器 + HTTP 服务。
- 推荐通过以下命令在本地启动：

```bash
python -m http.server 8080
```

然后打开：

```text
http://localhost:8080/index.html
```

## 5. 推荐部署方式

### 5.1 开发调试：优先推荐单文件方案

适合快速验证和减少手工文件数量。

推荐上传：

- `xiao.py` -> 设备上的 `main.py`
- `_autorun.py`
- `boot.py`（仅 USB 模式需要）
- `Servo.py`（仅产品功能或示例依赖时需要）

示例命令：

```bash
mpremote connect COM3 cp xiao.py :main.py
mpremote connect COM3 cp _autorun.py :_autorun.py
mpremote connect COM3 cp boot.py :boot.py
mpremote connect COM3 reset
```

说明：

- 如果不使用 USB，可以不上传 `boot.py`。
- 如果不使用舵机或相关示例，可以不上传 `Servo.py`。

### 5.2 开发调试：模块化方案

适合后续维护和分模块修改。

推荐上传：

- `main.py`
- `ble.py`
- `usb.py`
- `_autorun.py`
- `boot.py`（仅 USB 模式需要）

示例命令：

```bash
mpremote connect COM3 cp main.py :main.py
mpremote connect COM3 cp ble.py :ble.py
mpremote connect COM3 cp usb.py :usb.py
mpremote connect COM3 cp _autorun.py :_autorun.py
mpremote connect COM3 cp boot.py :boot.py
mpremote connect COM3 reset
```

注意：

- 当前 `main.py` 代码会直接导入 `ble.py`、`usb.py`、`_autorun.py`，因此部署时不应遗漏这些依赖文件。
- 当前代码中只有 `USB_ENABLED` 配置项；BLE 初始化仍然是默认启用状态。

### 5.3 前端部署

前端只需要部署一次，不需要跟每台设备一起烧录。

典型做法：

- 在研发电脑上用本地 HTTP 服务运行；
- 部署到公司内网服务器；
- 或后续封装成桌面工具。

建议浏览器：

- Chrome
- Edge

## 6. 工厂出厂时如何做

### 6.1 不建议的方式

不建议让产线人员手动执行以下动作：

- 手动挑选要上传哪些 `.py` 文件；
- 手动区分单文件方案和模块化方案；
- 手动决定是否需要 `boot.py` 或 `_autorun.py`；
- 手动打开浏览器排查 BLE / USB 是否工作。

这样只适合研发调试，不适合量产。

### 6.2 推荐的量产方式

量产时应改成“单一交付入口”：

- 工厂只执行一次烧录脚本；
- 标准产物由研发预先打包好；
- 工人不需要知道内部到底有没有 `ble.py`、`usb.py`。

推荐结构示例：

```text
factory-release/
  firmware/
    micropython.bin
  app/
    xiao.py
    _autorun.py
    boot.py
    Servo.py
  tools/
    flash_all.ps1
```

推荐流程：

```text
1. 刷写 MicroPython 固件
2. 上传设备端应用文件
3. 写入序列号 / 设备名 / 出厂配置
4. 重启设备
5. 执行冒烟测试
6. 输出日志并记录结果
```

### 6.3 为什么推荐先用 `xiao.py`

对于量产打包，`xiao.py` 更适合作为应用主文件来源，因为：

- 已经包含 BLE 和 USB 逻辑；
- 上传时只需重命名为设备上的 `main.py`；
- 能减少产线需要管理的文件数量。

这不代表研发阶段必须放弃模块化开发，而是表示：

- 开发时可以继续维护 `main.py`、`ble.py`、`usb.py`；
- 出厂时应收敛成统一交付方式。

## 7. 当前仓库需要注意的两个点

### 7.1 `_autorun.py` 属于当前代码的实际依赖

虽然 README 中原本的上传示例主要强调了 `main.py`、`ble.py`、`usb.py` 或 `xiao.py`，但当前代码实际已经依赖 `_autorun.py`。

因此当前版本部署时，建议一并上传 `_autorun.py`。

### 7.2 BLE 默认设备名与前端过滤条件暂不一致

当前设备端默认 BLE 名称前缀是：

```text
M200-
```

而前端扫描时使用的是固定名称：

```text
ESP32-CodeLoader
```

这意味着：

- 当前仓库直接走 BLE 首次联调时，可能搜不到设备；
- 建议首次调试优先使用 USB；
- 如果需要 BLE 开箱即用，应统一设备端默认广播名与前端扫描条件。

## 8. 建议的后续整理方向

如果后续要面向工厂和售后，建议继续整理为：

1. 一个标准设备端发布包；
2. 一个一键烧录脚本；
3. 一个产测步骤说明；
4. 一个最终面向使用者的前端部署入口。
