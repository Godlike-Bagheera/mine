"""Цветной вывод + запись в debug.log."""
from modules import debug
from modules import state  # noqa: F401

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BLUE = "\033[94m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def ok(msg):
    print(f"{GREEN}[✓]{RESET} {msg}")
    debug.dbg(msg, "OK")


def info(msg):
    print(f"{BLUE}[i]{RESET} {msg}")
    debug.dbg(msg)


def warn(msg):
    print(f"{YELLOW}[!]{RESET} {msg}")
    debug.dbg(msg, "WARN")


def err(msg):
    print(f"{RED}[✗]{RESET} {msg}")
    debug.dbg(msg, "ERR")


def hint(msg):
    print(f"{CYAN}[→]{RESET} {msg}")
    debug.dbg(msg, "HINT")


def fix(msg):
    print(f"{MAGENTA}[⚙]{RESET} {msg}")
    debug.dbg(msg, "FIX")


def sep():
    print(f"{BLUE}{'─' * 50}{RESET}")
    debug.dbg("---")
