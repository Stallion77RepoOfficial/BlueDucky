# utils/menu_functions.py

import os
import bluetooth
import re
import subprocess
import time
import logging as log

def restart_bluetooth_daemon():
    run(["sudo", "service", "bluetooth", "restart"])
    time.sleep(5)

def run(command):
    assert(isinstance(command, list))
    log.info("Executing command: '%s'" % " ".join(command))
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return result

def print_fancy_ascii_art():
    ascii_art = """
    (ASCII ART HERE)
    """
    print("\033[94m" + ascii_art + "\033[0m")  # Blue color

def clear_screen():
    os.system('clear')

# Function to save discovered devices to a file
def save_devices_to_file(devices, filename='known_devices.txt'):
    with open(filename, 'w') as file:
        for addr, name in devices:
            file.write(f"{addr},{name}\n")

# Function to load known devices from a file
def load_known_devices(filename='known_devices.txt'):
    if os.path.exists(filename):
        with open(filename, 'r') as file:
            return [tuple(line.strip().split(',')) for line in file]
    else:
        return []

def getterm():
    size = os.get_terminal_size()
    return size.columns

def print_menu():
    blue = '\033[94m'
    reset = "\033[0m"
    title = "BlueDucky - Bluetooth Device Attacker"
    vertext = "Ver 2.1"
    motd1 = "Remember, you can still attack devices without visibility.."
    motd2 = "If you have their MAC address.."
    terminal_width = getterm()
    separator = "=" * terminal_width

    print(blue + separator)  # Blue color for separator
    print(reset + title.center(len(separator)))  # Centered Title in blue
    print(blue + vertext.center(len(separator)))  # Centered Version
    print(blue + separator + reset)  # Blue color for separator
    print(motd1.center(len(separator)))  # Centered Message 1
    print(motd2.center(len(separator)))  # Centered Message 2
    print(blue + separator + reset)  # Blue color for separator

def main_menu():
    clear_screen()
    print_fancy_ascii_art()
    print_menu()

def is_valid_mac_address(mac_address):
    # Regular expression to match a MAC address in the form XX:XX:XX:XX:XX:XX
    mac_address_pattern = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$')
    return mac_address_pattern.match(mac_address) is not None

# Function to read DuckyScript from file
def read_duckyscript(filename):
    if os.path.exists(filename):
        with open(filename, 'r') as file:
            return [line.strip() for line in file.readlines()]
    else:
        log.warning(f"File {filename} not found. Skipping DuckyScript.")
        return None

# Function to scan for devices
def scan_for_devices(scan_time=15):
    main_menu()
    blue = "\033[94m"
    error = "\033[91m"
    reset = "\033[0m"

    # Load known devices
    known_devices = load_known_devices()
    if known_devices:
        print(f"\n{reset}Known devices{blue}:")
        for idx, (addr, name) in enumerate(known_devices):
            print(f"{blue}{idx + 1}{reset}: Device Name: {blue}{name}, Address: {blue}{addr}")

        use_known_device = input(f"\n{reset}Do you want to use one of these known devices{blue}? {blue}({reset}yes{blue}/{reset}no{blue}): ").strip().lower()
        if use_known_device in ['yes', 'y']:
            try:
                device_choice = int(input(f"{reset}Enter the index number of the device to attack{blue}: ")) - 1
                if 0 <= device_choice < len(known_devices):
                    return known_devices[device_choice][0]
                else:
                    print("\nInvalid selection.")
            except ValueError:
                print("\nInvalid input.")

    # Normal Bluetooth scan
    print(f"\n{reset}Attempting to scan for Bluetooth devices{blue}...")
    try:
        nearby_devices = bluetooth.discover_devices(duration=scan_time, lookup_names=True, flush_cache=True, lookup_class=True)
        device_list = []
        if len(nearby_devices) == 0:
            print(f"\n{reset}[{error}+{reset}] No nearby devices found.")
            return None
        else:
            print("\nFound {} nearby device(s):".format(len(nearby_devices)))
            for idx, (addr, name, dev_class) in enumerate(nearby_devices, 1):
                print(f"{idx}. {name} [{addr}] - Class: {dev_class}")
                device_list.append((addr, name))

        # Optionally save to known devices
        confirm_save = input(f"\n{reset}Do you want to save these devices to known devices{blue}? {blue}({reset}yes{blue}/{reset}no{blue}): ").strip().lower()
        if confirm_save in ['yes', 'y']:
            save_devices_to_file(device_list)
            print(f"\n{blue}Devices saved to known_devices.txt{reset}.")

        # Select a device
        while True:
            try:
                selection = int(input(f"\n{reset}Select a device by number{blue}: {blue}")) - 1
                if 0 <= selection < len(device_list):
                    target_address = device_list[selection][0]
                    selected_name = device_list[selection][1]
                    print(f"Selected target device: {selected_name} [{target_address}]")
                    return target_address
                else:
                    print("Invalid selection. Please try again.")
            except ValueError:
                print("Invalid input. Please enter a valid number.")
    except Exception as e:
        log.error(f"Error during scanning: {e}")
        return None
