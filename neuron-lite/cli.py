#!/usr/bin/env python3
"""
Satori Neuron CLI - Interactive command-line interface.
Starts the neuron in the background and provides commands to interact with it.
"""

import sys
import os
import time
import threading
import io
import logging as stdlib_logging
import tty
import termios

# Save original file descriptors BEFORE any redirection
_original_stdout_fd = os.dup(1)
_original_stderr_fd = os.dup(2)
_original_stdin_fd = os.dup(0)

# Create file objects from the duplicated fds
_console_out = os.fdopen(os.dup(_original_stdout_fd), 'w', buffering=1)
_console_in = os.fdopen(os.dup(_original_stdin_fd), 'r', buffering=1)

# Global log buffer
_log_buffer: list[str] = []
_max_logs = 1000

# Command history
_command_history: list[str] = []
_history_index: int = 0


class LogInterceptHandler(stdlib_logging.Handler):
    """Handler that captures all logs to buffer."""
    def emit(self, record):
        msg = self.format(record)
        timestamp = time.strftime("%H:%M:%S")
        _log_buffer.append(f"[{timestamp}] {msg}")
        if len(_log_buffer) > _max_logs:
            _log_buffer.pop(0)


class OutputCapture:
    """Captures stdout/stderr to buffer."""
    def __init__(self, original):
        self.original = original
        # Need fileno for subprocess compatibility
        self._devnull = open(os.devnull, 'w')

    def write(self, msg):
        msg = msg.strip()
        if msg:
            timestamp = time.strftime("%H:%M:%S")
            _log_buffer.append(f"[{timestamp}] {msg}")
            if len(_log_buffer) > _max_logs:
                _log_buffer.pop(0)

    def flush(self):
        pass

    def fileno(self):
        # Return devnull's fileno so subprocesses write there
        return self._devnull.fileno()


def setup_log_capture():
    """Set up log capture before importing neuron modules."""
    # Install our handler on root logger
    root = stdlib_logging.getLogger()
    root.handlers.clear()
    handler = LogInterceptHandler()
    handler.setFormatter(stdlib_logging.Formatter('%(levelname)s - %(message)s'))
    root.addHandler(handler)
    root.setLevel(stdlib_logging.DEBUG)

    # Capture stdout/stderr
    sys.stdout = OutputCapture(sys.__stdout__)
    sys.stderr = OutputCapture(sys.__stderr__)

    # Also redirect the actual file descriptors for subprocess output
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull_fd, 1)  # stdout
    os.dup2(devnull_fd, 2)  # stderr
    os.close(devnull_fd)


def console_print(msg: str = ""):
    """Print to actual console."""
    _console_out.write(msg + "\n")
    _console_out.flush()


def console_write(msg: str):
    """Write to actual console without newline."""
    _console_out.write(msg)
    _console_out.flush()


def console_readline() -> str:
    """Read a line from console."""
    return _console_in.readline()


def console_input(prompt: str = "") -> str:
    """Read input with arrow key support for command history."""
    global _history_index

    console_write(prompt)

    # Use the original stdin fd for reading
    fd = _original_stdin_fd
    old_settings = termios.tcgetattr(fd)

    try:
        tty.setraw(fd)

        line = ""
        cursor_pos = 0
        _history_index = len(_command_history)
        saved_line = ""  # Save current line when browsing history

        while True:
            # Read one character
            ch = os.read(fd, 1).decode('utf-8', errors='ignore')

            if ch == '\r' or ch == '\n':  # Enter
                console_write('\r\n')
                break
            elif ch == '\x03':  # Ctrl+C
                console_write('\r\n')
                raise KeyboardInterrupt
            elif ch == '\x04':  # Ctrl+D
                console_write('\r\n')
                raise EOFError
            elif ch == '\x7f' or ch == '\x08':  # Backspace
                if cursor_pos > 0:
                    line = line[:cursor_pos-1] + line[cursor_pos:]
                    cursor_pos -= 1
                    # Redraw line
                    console_write(f'\r{prompt}{line} \r{prompt}{line[:cursor_pos]}')
            elif ch == '\x1b':  # Escape sequence (arrow keys)
                seq1 = os.read(fd, 1).decode('utf-8', errors='ignore')
                if seq1 == '[':
                    seq2 = os.read(fd, 1).decode('utf-8', errors='ignore')
                    if seq2 == 'A':  # Up arrow - previous command
                        if _command_history and _history_index > 0:
                            if _history_index == len(_command_history):
                                saved_line = line
                            _history_index -= 1
                            line = _command_history[_history_index]
                            cursor_pos = len(line)
                            # Clear and redraw
                            console_write(f'\r{prompt}{" " * 50}\r{prompt}{line}')
                    elif seq2 == 'B':  # Down arrow - next command
                        if _history_index < len(_command_history):
                            _history_index += 1
                            if _history_index == len(_command_history):
                                line = saved_line
                            else:
                                line = _command_history[_history_index]
                            cursor_pos = len(line)
                            # Clear and redraw
                            console_write(f'\r{prompt}{" " * 50}\r{prompt}{line}')
                    elif seq2 == 'C':  # Right arrow
                        if cursor_pos < len(line):
                            cursor_pos += 1
                            console_write('\x1b[C')
                    elif seq2 == 'D':  # Left arrow
                        if cursor_pos > 0:
                            cursor_pos -= 1
                            console_write('\x1b[D')
            elif ch >= ' ':  # Printable character
                line = line[:cursor_pos] + ch + line[cursor_pos:]
                cursor_pos += 1
                # Redraw from cursor
                console_write(f'{line[cursor_pos-1:]}\r{prompt}{line[:cursor_pos]}')

        # Add to history if non-empty
        if line.strip() and (not _command_history or _command_history[-1] != line):
            _command_history.append(line)

        return line

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


class NeuronCLI:
    """Interactive CLI for Satori Neuron."""

    def __init__(self, env: str = 'prod', runMode: str = 'worker'):
        self.env = env
        self.runMode = runMode
        self.startup = None
        self.neuron_started = False
        self._vault_password_set = False

    def add_log(self, message: str):
        """Add a log message to buffer."""
        timestamp = time.strftime("%H:%M:%S")
        _log_buffer.append(f"[{timestamp}] {message}")

    def check_vault_password_exists(self) -> bool:
        """Check if vault password exists in config."""
        from satorineuron import config
        vault_password = config.get().get('vault password')
        return vault_password is not None and len(str(vault_password)) > 0

    def prompt_mandatory_vault_password(self) -> bool:
        """Prompt user to create a vault password. Returns True if successful."""
        console_print()
        console_print("=" * 60)
        console_print("  VAULT PASSWORD SETUP REQUIRED")
        console_print("=" * 60)
        console_print()
        console_print("A vault password is required to secure your wallet.")
        console_print("This password encrypts your private keys and funds.")
        console_print()
        console_print("IMPORTANT: Save this password in a secure location!")
        console_print("If you lose this password, you will lose access to your vault.")
        console_print("There is no way to recover a lost vault password.")
        console_print()
        console_print("=" * 60)
        console_print()

        while True:
            console_write("Enter new vault password (min 4 characters): ")
            password1 = console_readline().strip()

            if len(password1) < 4:
                console_print("Password must be at least 4 characters. Please try again.")
                console_print()
                continue

            console_write("Confirm password: ")
            password2 = console_readline().strip()

            if password1 != password2:
                console_print("Passwords do not match. Please try again.")
                console_print()
                continue

            # Save password to config
            from satorineuron import config
            config.add(data={'vault password': password1})
            self._vault_password_set = True

            console_print()
            console_print("Vault password saved successfully!")
            console_print()
            console_print("REMINDER: Please save your password securely!")
            console_print("         You will need it to access your vault.")
            console_print()
            return True

    def start_neuron_background(self):
        """Start the neuron in a background thread."""
        def run():
            try:
                # Import here after log capture is set up
                from start import StartupDag
                self.add_log("Creating StartupDag...")
                self.startup = StartupDag.create(
                    env=self.env,
                    runMode=self.runMode)
                self.neuron_started = True
                self.add_log("Neuron started successfully")
            except Exception as e:
                self.add_log(f"Neuron startup error: {e}")
                import traceback
                self.add_log(traceback.format_exc())

        self.add_log("Starting neuron in background...")
        threading.Thread(target=run, daemon=True).start()

    def handle_command(self, user_input: str) -> str | None:
        """Process user commands and return response."""
        user_input = user_input.strip()

        if user_input == "/help":
            return """Available commands:
  /logs          - View logs sub-menu
  /status        - Show current status
  /balance       - Show wallet balance
  /streams       - Show stream assignments
  /pause         - Pause the engine
  /unpause       - Unpause the engine
  /restart       - Restart the neuron
  /stake         - Check stake status
  /pool          - Show pool status
  /vault-create  - Create a new vault with password
  /vault-open    - Open existing vault with password
  /vault-status  - Check vault status
  /clear         - Clear the screen
  /help          - Show this help message
  /exit          - Exit CLI (neuron keeps running)"""

        elif user_input == "/logs":
            return """Logs sub-menu:
  /logs neuron   - Show neuron logs
  /logs engine   - Show engine logs"""

        elif user_input == "/logs neuron":
            logs = _log_buffer[-50:]
            if not logs:
                return "No logs yet."
            return "\n".join(logs)

        elif user_input == "/logs engine":
            # Filter logs for engine-related messages
            engine_keywords = [
                'engine', 'Engine', 'adapter', 'Adapter', 'prediction',
                'Prediction', 'StreamModel', 'stream model', 'forecast',
                'XgbAdapter', 'StarterAdapter', 'XgbChronosAdapter',
                'model training', 'inference', 'Engine DB'
            ]
            engine_logs = [
                log for log in _log_buffer
                if any(keyword in log for keyword in engine_keywords)
            ]
            logs = engine_logs[-50:]
            if not logs:
                return "No engine logs yet."
            return "\n".join(logs)

        elif user_input == "/status":
            if not self.neuron_started:
                return "Neuron is starting... Use /logs neuron to see progress."
            status_lines = [
                f"Mode: {self.startup.runMode.name}",
                f"Paused: {self.startup.paused}",
                f"Mining Mode: {self.startup.miningMode}",
                f"Environment: {self.startup.env}",
                f"Version: {self.startup.version}",
            ]
            if self.startup.wallet:
                status_lines.append(f"Wallet: {self.startup.wallet.address}")
            return "\n".join(status_lines)

        elif user_input == "/balance":
            if not self.neuron_started:
                return "Neuron is starting... Use /logs neuron to see progress."
            balance_lines = [
                f"Holding Balance: {self.startup.holdingBalance}",
                f"Server Balance: {self.startup.getBalance('currency')}",
            ]
            if self.startup.balances:
                for k, v in self.startup.balances.items():
                    balance_lines.append(f"  {k}: {v}")
            return "\n".join(balance_lines)

        elif user_input == "/streams":
            if not self.neuron_started:
                return "Neuron is starting... Use /logs neuron to see progress."
            lines = [f"Subscriptions: {len(self.startup.subscriptions)}"]
            for s in self.startup.subscriptions[:5]:
                lines.append(f"  - {s.streamId.source}/{s.streamId.stream}")
            if len(self.startup.subscriptions) > 5:
                lines.append(f"  ... and {len(self.startup.subscriptions) - 5} more")
            lines.append(f"Publications: {len(self.startup.publications)}")
            for p in self.startup.publications[:5]:
                lines.append(f"  - {p.streamId.source}/{p.streamId.stream}")
            if len(self.startup.publications) > 5:
                lines.append(f"  ... and {len(self.startup.publications) - 5} more")
            return "\n".join(lines)

        elif user_input == "/pause":
            if not self.neuron_started:
                return "Neuron is starting... Use /logs neuron to see progress."
            self.startup.pause()
            return "Engine paused"

        elif user_input == "/unpause":
            if not self.neuron_started:
                return "Neuron is starting... Use /logs neuron to see progress."
            self.startup.unpause()
            return "Engine unpaused"

        elif user_input == "/restart":
            if not self.neuron_started:
                return "Neuron is starting... Use /logs neuron to see progress."
            self.startup.triggerRestart()

        elif user_input == "/stake":
            if not self.neuron_started:
                return "Neuron is starting... Use /logs neuron to see progress."
            status = self.startup.performStakeCheck()
            return f"Stake Status: {status}\nStake Required: {self.startup.stakeRequired}"

        elif user_input == "/pool":
            if not self.neuron_started:
                return "Neuron is starting... Use /logs neuron to see progress."
            return f"Pool Accepting: {self.startup.poolIsAccepting}"

        elif user_input == "/vault-status":
            if not self.neuron_started:
                return "Neuron is starting... Use /logs neuron to see progress."
            vault = self.startup.vault
            if vault is None:
                return "Vault: Not created\nUse /vault-create to create a new vault."
            status_lines = [
                f"Vault Address: {vault.address}",
                f"Decrypted: {vault.isDecrypted if hasattr(vault, 'isDecrypted') else 'Unknown'}",
            ]
            return "\n".join(status_lines)

        elif user_input == "/vault-create":
            if not self.neuron_started:
                return "Neuron is starting... Use /logs neuron to see progress."
            if self.startup.vault is not None:
                return "Vault already exists. Use /vault-open to unlock it."
            return "VAULT_CREATE_PROMPT"

        elif user_input == "/vault-open":
            if not self.neuron_started:
                return "Neuron is starting... Use /logs neuron to see progress."
            vault = self.startup.vault
            if vault is None:
                return "No vault exists. Use /vault-create to create one first."
            if hasattr(vault, 'isDecrypted') and vault.isDecrypted:
                return "Vault is already open."
            return "VAULT_OPEN_PROMPT"

        elif user_input == "/clear":
            # ANSI escape code to clear screen and move cursor to top
            console_write("\033[2J\033[H")
            return "Satori Neuron CLI. Type /help for commands."

        elif user_input in ("/exit", "/quit"):
            return "EXIT_CLI"

        elif user_input.startswith("/"):
            return f"Unknown command: {user_input}. Type /help for available commands."

        else:
            return "Type /help for available commands."

    def prompt_vault_password(self, create: bool = False) -> str | None:
        """Prompt for vault password securely."""
        import getpass
        try:
            if create:
                console_print("Creating new vault...")
                console_write("Enter new vault password: ")
                password1 = console_readline().strip()
                console_write("Confirm password: ")
                password2 = console_readline().strip()
                if password1 != password2:
                    return None, "Passwords do not match."
                if len(password1) < 4:
                    return None, "Password must be at least 4 characters."
                return password1, None
            else:
                console_write("Enter vault password: ")
                password = console_readline().strip()
                return password, None
        except Exception as e:
            return None, str(e)

    def create_vault(self, password: str) -> str:
        """Create a new vault with the given password."""
        try:
            # Save password to config
            from satorineuron import config
            config.add(data={'vault password': password})

            # Create vault
            vault = self.startup.openVault(password=password, create=True)
            if vault:
                return f"Vault created successfully!\nVault Address: {vault.address}"
            return "Failed to create vault."
        except Exception as e:
            return f"Error creating vault: {e}"

    def open_vault(self, password: str) -> str:
        """Open an existing vault with the given password."""
        try:
            vault = self.startup.openVault(password=password)
            if vault and hasattr(vault, 'isDecrypted') and vault.isDecrypted:
                return f"Vault opened successfully!\nVault Address: {vault.address}"
            return "Failed to open vault. Wrong password?"
        except Exception as e:
            return f"Error opening vault: {e}"

    def run(self):
        """Run interactive CLI loop."""
        console_print("Satori Neuron CLI. Type /help for commands, /exit to quit.")
        console_print()

        # Check if vault password exists, if not, prompt for mandatory creation
        if not self.check_vault_password_exists():
            if not self.prompt_mandatory_vault_password():
                console_print("Vault password is required. Exiting.")
                return

        # Start neuron in background
        self.start_neuron_background()

        while True:
            try:
                # Use console_input() for readline support (arrow keys, history)
                user_input = console_input("> ").strip()

                if not user_input:
                    continue

                response = self.handle_command(user_input)

                if response == "EXIT_CLI":
                    console_print("Goodbye!")
                    break

                if response == "VAULT_CREATE_PROMPT":
                    password, error = self.prompt_vault_password(create=True)
                    if error:
                        console_print(error)
                    elif password:
                        result = self.create_vault(password)
                        console_print(result)
                    console_print()
                    continue

                if response == "VAULT_OPEN_PROMPT":
                    password, error = self.prompt_vault_password(create=False)
                    if error:
                        console_print(error)
                    elif password:
                        result = self.open_vault(password)
                        console_print(result)
                    console_print()
                    continue

                console_print(response)
                console_print()

            except KeyboardInterrupt:
                console_print("\nGoodbye!")
                break
            except EOFError:
                console_print("\nGoodbye!")
                break


if __name__ == "__main__":
    # Set up log capture FIRST before any imports
    setup_log_capture()

    cli = NeuronCLI(env='prod', runMode='worker')
    cli.run()
