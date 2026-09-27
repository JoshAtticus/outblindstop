import os
import sys

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"

FRAMEWORK = "M2xxx/M3xxx"


def clear():
    print("\033[2J\033[H", end="")


def banner():
    print(rf"""{CYAN}{BOLD}
              _   _     _ _           _     _              
   ___  _   _| |_| |__ | (_)_ __   __| |___| |_ ___  _ __  
  / _ \| | | | __| '_ \| | | '_ \ / _` / __| __/ _ \| '_ \ 
 | (_) | |_| | |_| |_) | | | | | | (_| \__ \ || (_) | |_) |
  \___/ \__,_|\__|_.__/|_|_|_| |_|\__,_|___/\__\___/| .__/ 
                                                    |_|    
{RESET}{DIM}                    Inseego {FRAMEWORK} tool (root / backup / tweak){RESET}""")


def fail(msg):
    print(f"{RED}{BOLD}ERROR: {msg}{RESET}")


def ok(msg):
    print(f"{GREEN}{msg}{RESET}")


def info(msg):
    print(f"{CYAN}{msg}{RESET}")


def warn(msg):
    print(f"{YELLOW}{msg}{RESET}")


def read_key():
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            arrow = msvcrt.getch().decode(errors="ignore")
            return {"H": "up", "P": "down"}.get(arrow, "")
        if ch == b"\r":
            return "enter"
        if ch == b"\x03":
            return "ctrlc"
        return ch.decode(errors="ignore")
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if ch == "\x1b":
        return {"A": "up", "B": "down"}.get(sys.stdin.read(1), "")
    return {"\r": "enter", "\n": "enter", "\x03": "ctrlc"}.get(ch, ch)


def menu(title, options, info_lines=()):
    sel = 0
    start = 0
    while True:
        clear()
        banner()
        print(f"{BOLD}{MAGENTA}  {title}{RESET}\n")
        for line in info_lines:
            print(f"  {line}")
        print()
        try:
            rows = os.get_terminal_size().lines
        except OSError:
            rows = 30
        visible = max(4, rows - len(info_lines) - 18)
        if sel < start:
            start = sel
        elif sel >= start + visible:
            start = sel - visible + 1
        end = start + visible
        if start > 0:
            print(f"  {DIM}... {start} more above{RESET}")
        for i in range(start, min(end, len(options))):
            if i == sel:
                print(f"  {CYAN}{BOLD} > {options[i]}{RESET}")
            else:
                print(f"    {DIM}{options[i]}{RESET}")
        if end < len(options):
            print(f"  {DIM}... {len(options) - end} more below{RESET}")
        print(f"\n  {DIM}[arrows] move   [enter] select   [ctrl+c] back{RESET}")
        key = read_key()
        if key == "ctrlc":
            return -1
        if key == "up":
            sel = (sel - 1) % len(options)
        elif key == "down":
            sel = (sel + 1) % len(options)
        elif key == "enter":
            return sel


def prompt(msg, default=None):
    try:
        val = input(f"  {CYAN}{msg}{RESET}").strip()
    except EOFError:
        return default
    return val or default


def multi_menu(title, options, info_lines=()):
    if os.name == "nt":
        import msvcrt
        getch = msvcrt.getch
    else:
        getch = None
    picked = set()
    sel = 0
    hint = ""
    while True:
        clear()
        banner()
        print(f"{BOLD}{MAGENTA}  {title}{RESET}\n")
        for line in info_lines:
            print(f"  {line}")
        print()
        for i, opt in enumerate(options):
            box = "[x]" if i in picked else "[ ]"
            if i == sel:
                print(f"  {CYAN}{BOLD} > {box} {opt}{RESET}")
            else:
                print(f"    {DIM}{box} {opt}{RESET}")
        if hint:
            print(f"\n  {YELLOW}{hint}{RESET}")
        print(f"\n  {DIM}[arrows] move   [space] toggle   [enter] done   [ctrl+c] back{RESET}")
        if os.name == "nt":
            ch = getch()
            if ch in (b"\x00", b"\xe0"):
                arrow = getch().decode(errors="ignore")
                key = {"H": "up", "P": "down"}.get(arrow, "")
            elif ch == b"\r":
                key = "enter"
            elif ch == b"\x03":
                return []
            elif ch == b" ":
                key = "space"
            else:
                key = ""
        else:
            import termios
            import tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                ch = sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            if ch == "\x1b":
                key = {"A": "up", "B": "down"}.get(sys.stdin.read(1), "")
            elif ch in ("\r", "\n"):
                key = "enter"
            elif ch == "\x03":
                return []
            elif ch == " ":
                key = "space"
            else:
                key = ""
        if key == "up":
            sel = (sel - 1) % len(options)
            hint = ""
        elif key == "down":
            sel = (sel + 1) % len(options)
            hint = ""
        elif key == "space":
            picked.symmetric_difference_update({sel})
            hint = ""
        elif key == "enter":
            if not picked:
                hint = "nothing ticked yet - press space on the items you want"
                continue
            return [options[i] for i in sorted(picked)]


def pause(msg="press enter to continue"):
    try:
        input(f"\n  {DIM}{msg}...{RESET}")
    except EOFError:
        pass
