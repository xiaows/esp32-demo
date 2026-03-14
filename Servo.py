from machine import UART, Pin
import time


class Motors:
    COMMANDS = {
        "TurnOffTorque": b"\xF9\xFF\xFE\x04\x03\x64\x00\xCC",
        "TurnOnTorque": b"\xF9\xFF\xFE\x04\x03\x64\x02\xCC",
        "ServosMode": b"\xF9\xFF\x01\x04\x03\x5A\x01\xCC",
        "MotorMode": b"\xF9\xFF\xFD\x04\x03\x5A\x02\xCC",
        "ServoPositionReset": b"\xF9\xFF\x01\x04\x03\x1D\x01\xD9",
        "GetCurrentLocation": b"\xF9\xFF\x01\x03\x02\x46\xB3",
        "GetCurrentSpeed": b"\xF9\xFF\x01\x03\x02\x47\xB2",
        "GetID": b"\xF9\xFF\x01\x03\x02\x0F\xCC",
        "SetID": b"\xF9\xFF\x01\x04\x03\x0F\x02\xF4",
        "SetBot": b"\xF9\xFF\x01\x05\x03\x10\x18\x00\xCE",
        "GetBot": b"\xF9\xFF\x01\x03\x02\x10\xCC",
        "Ping": b"\xFF\xFF\x01\x02\x01\xFB",
        "Querry": b"\xF9\xFF\x01\x02\x01\xFB",
    }

    def __init__(self, tx=17, rx=16, in0=33, in1=25):
        self.uart = UART(2, baudrate=115200, bits=8, stop=1, parity=None, tx=tx, rx=rx)
        self.in0 = Pin(in0, Pin.OUT)
        self.in1 = Pin(in1, Pin.OUT)

    def _select_port(self, port):
        if port == 1:
            self.in0.value(0); self.in1.value(0)
        elif port == 2:
            self.in0.value(1); self.in1.value(0)
        elif port == 3:
            self.in0.value(0); self.in1.value(1)
        elif port == 4:
            self.in0.value(1); self.in1.value(1)

    def _send_cmd(self, name, speed=None):
        if name in self.COMMANDS:
            data = self.COMMANDS[name]
            self.uart.write(data)
            print(f"Data sent: {name} - {data.hex()}")
        elif name == "SetMotorSpeed" and speed is not None:
            data = self._motor_speed_cmd(speed)
            self.uart.write(data)
            print(f"Data sent: SetMotorSpeed - {data.hex()}")

    def _setup_and_send(self, mode, action=None):
        self._send_cmd("TurnOffTorque")
        self._send_cmd(mode)
        self._send_cmd("TurnOnTorque")
        if action:
            action()

    def _motor_speed_cmd(self, speed):
        return b"\xF9\xFF\x01\x05\x03\x6E" + speed.to_bytes(2, "little") + b"\xCC"

    def _servo_relative_angle(self, angle):
        v = int(angle * 10)
        b = v.to_bytes(4, "little")
        cmd = b"\xF9\xFF\x01\x0D\x03\x67\x11\x00" + b[:2] + b[2:] + b"\x84\x03\x00\x00\xCC"
        self.uart.write(cmd)
        print(f"Data sent: ServoRelativeAngle - {cmd.hex()}")

    def _servo_degree(self, circles, clockwise=True):
        deg = int(circles * 3600)
        if not clockwise:
            deg = -deg
        if deg < 0:
            deg += 2**32
        lo = (deg & 0xFFFF).to_bytes(2, "little")
        hi = ((deg >> 16) & 0xFFFF).to_bytes(2, "little")
        cmd = b"\xF9\xFF\x01\x0D\x03\x67\x11\x00" + lo + hi + b"\x84\x03\x00\x00\xCC"
        self.uart.write(cmd)
        print(f"Data sent: ServoDegree - {cmd.hex()}")

    def _servo_relative_seconds(self, seconds, clockwise=True):
        v = int(seconds * 10)
        if v < 0:
            v += 2**16
        sb = v.to_bytes(2, "little")
        d = 0x14 if clockwise else 0x15
        cmd = b"\xF9\xFF\x01\x09\x03\x70" + bytes([d]) + b"\x00" + sb + b"\x84\x03\xCC"
        self.uart.write(cmd)
        print(f"Data sent: ServoRelativeSeconds - {cmd.hex()}")

    def _absolute_angle(self, angle):
        v = int(angle * 10)
        if v < 0:
            v += 2**32
        b = v.to_bytes(4, "little")
        cmd = b"\xF9\xFF\x01\x07\x03\x65" + b[:2] + b"\x00\x00\xCC"
        self.uart.write(cmd)
        print(f"Data sent: AbsoluteAngle - {cmd.hex()}")

    def run_specified_units(self, port, direction, amount, form):
        """
        port: 1=A, 2=B, 3=C, 4=D
        direction: 0=forward, 1=reverse
        form: 1=rotation, 2=degree, 3=seconds
        """
        self._select_port(port)
        cw = (direction == 0)
        if form == 1:
            self._setup_and_send("ServosMode", lambda: self._servo_degree(amount, clockwise=cw))
        elif form == 2:
            a = amount if cw else -amount
            self._setup_and_send("ServosMode", lambda: self._servo_relative_angle(a))
        elif form == 3:
            self._setup_and_send("MotorMode", lambda: self._servo_relative_seconds(amount, clockwise=cw))

    def set_motor_speed(self, port, speed):
        self._select_port(port)
        self._setup_and_send("MotorMode", lambda: self._send_cmd("SetMotorSpeed", speed))

    def set_absolute_angle(self, port, angle):
        self._select_port(port)
        self._setup_and_send("ServosMode", lambda: self._absolute_angle(angle))

    def reset_position(self, port):
        self._select_port(port)
        self._setup_and_send("ServosMode", lambda: self._send_cmd("ServoPositionReset"))

    def get_position(self, port):
        self._select_port(port)
        self._send_cmd("GetCurrentLocation")
        time.sleep(0.1)
        if self.uart.any():
            resp = self.uart.read()
            if (len(resp) >= 10 and resp[0:2] == b"\xF9\xF5"
                    and resp[3] == 0x07 and resp[5] == 0x46):
                pos = int.from_bytes(resp[6:10], "little")
                if pos > 2**31:
                    pos -= 2**32
                result = (pos / 10) % 360
                print(f"Position: {result}")
                return result
        return None

    def get_speed(self, port):
        self._select_port(port)
        self._send_cmd("GetCurrentSpeed")
        time.sleep(0.1)
        if self.uart.any():
            resp = self.uart.read()
            if (len(resp) >= 9 and resp[0:2] == b"\xF9\xF5"
                    and resp[3] == 0x05 and resp[5] == 0x47):
                speed = int.from_bytes(resp[6:8], "little")
                print(f"Speed: {speed}%")
                return speed
        return None


# 预实例化，import 即用
motors = Motors()
