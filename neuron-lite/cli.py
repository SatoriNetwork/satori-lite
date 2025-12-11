#!/usr/bin/env python3
"""
Satori Neuron CLI - Interactive command-line interface for wallet and vault management.
Provides commands to check balances and manage vault.
"""

import sys
import os
import time
import tty
import termios

# Save original file descriptors for console I/O
_original_stdout_fd = os.dup(1)
_original_stdin_fd = os.dup(0)

# Create file objects from the duplicated fds
_console_out = os.fdopen(os.dup(_original_stdout_fd), 'w', buffering=1)
_console_in = os.fdopen(os.dup(_original_stdin_fd), 'r', buffering=1)

# Command history
_command_history: list[str] = []
_history_index: int = 0


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
        self.wallet_manager = None
        self._vault_password = None  # Store password in memory for session

    def check_vault_file_exists(self) -> bool:
        """Check if vault.yaml file exists (like web UI does)."""
        try:
            from satorineuron import config
            vault_path = config.walletPath('vault.yaml')
            return os.path.exists(vault_path)
        except Exception:
            return False

    def check_vault_password_exists(self) -> bool:
        """Check if vault password exists in config."""
        from satorineuron import config
        vault_password = config.get().get('vault password')
        return vault_password is not None and len(str(vault_password)) > 0

    def prompt_mandatory_vault_password(self) -> bool:
        """Prompt user to create a vault password. Returns True if successful."""
        # Check if vault password already exists (could be set via web UI or another CLI instance)
        if self.check_vault_password_exists():
            console_print()
            console_print("=" * 60)
            console_print("  VAULT PASSWORD ALREADY EXISTS")
            console_print("=" * 60)
            console_print()
            console_print("A vault password has already been created.")
            console_print("You can use it to unlock your vault.")
            console_print()
            console_print("If you set it in another interface (web UI), use that password.")
            console_print("=" * 60)
            console_print()
            return True

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

            # Double-check if password was created elsewhere (race condition protection)
            if self.check_vault_password_exists():
                console_print()
                console_print("Vault password was already created by another process.")
                console_print("Please use the existing password to unlock your vault.")
                console_print()
                return True

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

    def prompt_vault_setup_or_unlock(self) -> bool:
        """
        Prompt user to either create new vault or unlock existing vault.
        Returns True if successful, False otherwise.
        This matches the web UI behavior - passwords are NOT saved to config.
        """
        from satorineuron import config

        # Check if config password exists (backward compatibility)
        config_password = config.get().get('vault password')

        if not self.check_vault_file_exists():
            # No vault file - need to create new vault
            console_print()
            console_print("=" * 60)
            console_print("  VAULT SETUP")
            console_print("=" * 60)
            console_print()
            console_print("No vault found. Let's create a new one.")
            console_print()
            console_print("Create a secure password to protect your vault.")
            console_print("This password encrypts your private keys and funds.")
            console_print()
            console_print("IMPORTANT: Save this password in a secure location!")
            console_print("If you lose this password, you will lose access to your vault.")
            console_print("There is no way to recover a lost vault password.")
            console_print()
            console_print("=" * 60)
            console_print()

            return self.setup_new_vault()
        else:
            # Vault file exists - need to unlock it
            console_print()
            console_print("=" * 60)
            console_print("  VAULT UNLOCK")
            console_print("=" * 60)
            console_print()

            # Try config password first (backward compatibility, like web UI)
            if config_password:
                console_print("Attempting to unlock vault with config password...")
                if self.unlock_existing_vault(config_password):
                    console_print("Vault unlocked successfully!")
                    console_print()
                    return True
                else:
                    console_print("Config password failed. Please enter your password manually.")
                    console_print()

            console_print("Please enter your vault password to unlock.")
            console_print("(Press Ctrl+C to exit)")
            console_print()
            console_print("=" * 60)
            console_print()

            # Prompt for password (unlimited attempts, like web UI)
            while True:
                console_write("Enter vault password: ")
                password = console_readline().strip()

                if self.unlock_existing_vault(password):
                    console_print()
                    console_print("Vault unlocked successfully!")
                    console_print()
                    return True
                else:
                    console_print("Invalid password. Please try again.")
                    console_print()

    def setup_new_vault(self) -> bool:
        """
        Create a new vault with password (DO NOT save password to config).
        Matches web UI behavior.
        """
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

            # Try to create the vault using WalletManager
            try:
                from satorineuron.init.wallet import WalletManager

                # Create wallet manager without using config password
                wallet_manager = WalletManager.create(useConfigPassword=False)
                vault = wallet_manager.openVault(password=password1, create=True)

                if vault and vault.isDecrypted:
                    self._vault_password = password1  # Store in memory for session
                    console_print()
                    console_print("Vault created successfully!")
                    console_print()
                    console_print("REMINDER: Please save your password securely!")
                    console_print("         You will need it to access your vault.")
                    console_print()
                    return True
                else:
                    console_print("Error creating vault. Please try again.")
                    console_print()
                    return False
            except Exception as e:
                console_print(f"Error creating vault: {e}")
                console_print()
                return False

    def unlock_existing_vault(self, password: str) -> bool:
        """
        Unlock existing vault with password.
        Returns True if successful, False otherwise.
        """
        try:
            from satorineuron.init.wallet import WalletManager

            # Create wallet manager without using config password
            wallet_manager = WalletManager.create(useConfigPassword=False)
            vault = wallet_manager.openVault(password=password, create=False)

            if vault and vault.isDecrypted:
                self._vault_password = password  # Store in memory for session
                return True
            else:
                return False
        except Exception:
            return False


    def interactive_menu(self, title: str, options: list[dict]) -> int | None:
        """
        Display an interactive menu with arrow key navigation.

        Args:
            title: Menu title to display
            options: List of dicts with 'label' and 'action' keys
                     e.g., [{'label': 'Option 1', 'action': callable}, ...]

        Returns:
            Index of selected option, or None if cancelled
        """
        selected = 0

        # Use the original stdin fd for reading
        fd = _original_stdin_fd
        old_settings = termios.tcgetattr(fd)

        try:
            tty.setraw(fd)

            # Initial render
            console_write('\r\n')
            console_write(title + '\r\n')
            console_write("-" * len(title) + '\r\n')
            for i, option in enumerate(options):
                if i == selected:
                    console_write(f"  > \x1b[7m{option['label']}\x1b[0m\r\n")  # Highlighted
                else:
                    console_write(f"    {option['label']}\r\n")
            console_write('\r\n')
            console_write("(Use ↑/↓ arrows to navigate, Enter to select, Esc to cancel)\r\n")

            # Number of lines printed (title + separator + options + blank + help text)
            lines_printed = 2 + len(options) + 1 + 1

            while True:
                # Read one character
                ch = os.read(fd, 1).decode('utf-8', errors='ignore')

                if ch == '\r' or ch == '\n':  # Enter - select
                    # Clear menu
                    console_write(f'\x1b[{lines_printed}A')  # Move cursor up
                    for _ in range(lines_printed):
                        console_write('\x1b[2K\n')  # Clear line and move down
                    console_write(f'\x1b[{lines_printed}A')  # Move back up
                    return selected

                elif ch == '\x03':  # Ctrl+C - cancel
                    # Clear menu
                    console_write(f'\x1b[{lines_printed}A')
                    for _ in range(lines_printed):
                        console_write('\x1b[2K\n')
                    console_write(f'\x1b[{lines_printed}A')
                    raise KeyboardInterrupt

                elif ch == '\x1b':  # Escape sequence
                    seq1 = os.read(fd, 1).decode('utf-8', errors='ignore')
                    if seq1 == '[':
                        seq2 = os.read(fd, 1).decode('utf-8', errors='ignore')

                        old_selected = selected

                        if seq2 == 'A':  # Up arrow
                            selected = max(0, selected - 1)
                        elif seq2 == 'B':  # Down arrow
                            selected = min(len(options) - 1, selected + 1)

                        # If selection changed, redraw
                        if old_selected != selected:
                            # Move cursor back to start of menu
                            console_write(f'\x1b[{lines_printed}A')

                            # Redraw menu
                            console_write('\x1b[2K')  # Clear title line
                            console_write(title + '\r\n')
                            console_write('\x1b[2K')
                            console_write("-" * len(title) + '\r\n')
                            for i, option in enumerate(options):
                                console_write('\x1b[2K')  # Clear line
                                if i == selected:
                                    console_write(f"  > \x1b[7m{option['label']}\x1b[0m\r\n")
                                else:
                                    console_write(f"    {option['label']}\r\n")
                            console_write('\x1b[2K\r\n')  # Clear blank line
                            console_write('\x1b[2K')
                            console_write("(Use ↑/↓ arrows to navigate, Enter to select, Esc to cancel)\r\n")

                    elif seq1 == '\x1b':  # Double escape (Esc key) - cancel
                        # Clear menu
                        console_write(f'\x1b[{lines_printed}A')
                        for _ in range(lines_printed):
                            console_write('\x1b[2K\n')
                        console_write(f'\x1b[{lines_printed}A')
                        return None

        except KeyboardInterrupt:
            return None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def get_wallet_balance_electrumx(self) -> str:
        """Fetch wallet balance from ElectrumX servers."""
        if not self.wallet_manager:
            return "Wallet manager not initialized."

        console_print("Fetching wallet balance...")
        console_print()

        try:
            # Ensure ElectrumX connection with retry
            if hasattr(self.wallet_manager, 'connect'):
                for _ in range(3):
                    if self.wallet_manager.connect():
                        break
                    time.sleep(1)

            # Get wallet
            wallet = self.wallet_manager.wallet
            if not wallet:
                return "Wallet not available."

            # Fetch balances from ElectrumX (retry if connection not ready)
            if hasattr(wallet, 'getBalances'):
                for _ in range(3):
                    if wallet.electrumx and wallet.electrumx.connected():
                        wallet.getBalances()
                        break
                    time.sleep(1)
                else:
                    # Final attempt even if connection check failed
                    wallet.getBalances()

            # Format and return balance information
            lines = ["Wallet Balance:"]
            lines.append("-" * 40)

            # SATORI balance
            if hasattr(wallet, 'balance') and wallet.balance:
                satori_amount = wallet.balance.amount
                lines.append(f"  SATORI: {satori_amount:.8f}")
            else:
                lines.append("  SATORI: 0.00000000")

            # EVR balance
            if hasattr(wallet, 'currency') and wallet.currency:
                evr_amount = wallet.currency.amount
                lines.append(f"  EVR:    {evr_amount:.8f}")
            else:
                lines.append("  EVR:    0.00000000")

            return "\n".join(lines)

        except Exception as e:
            return f"Error fetching wallet balance: {e}"

    def get_vault_balance_electrumx(self) -> str:
        """Fetch vault balance from ElectrumX servers."""
        if not self.wallet_manager:
            return "Wallet manager not initialized."

        console_print("Fetching vault balance...")
        console_print()

        try:
            # Ensure ElectrumX connection with retry
            if hasattr(self.wallet_manager, 'connect'):
                for _ in range(3):
                    if self.wallet_manager.connect():
                        break
                    time.sleep(1)

            # Re-open vault with stored password to ensure it's decrypted
            if self._vault_password:
                vault = self.wallet_manager.openVault(password=self._vault_password)
            else:
                vault = self.wallet_manager.vault

            if not vault:
                return "Vault not available.\nPlease create or unlock your vault first."

            # Check if vault is decrypted
            if hasattr(vault, 'isDecrypted') and not vault.isDecrypted:
                return "Vault is locked.\nPlease unlock your vault first."

            # Fetch balances from ElectrumX (retry if connection not ready)
            if hasattr(vault, 'getBalances'):
                for _ in range(3):
                    if vault.electrumx and vault.electrumx.connected():
                        vault.getBalances()
                        break
                    time.sleep(1)
                else:
                    # Final attempt even if connection check failed
                    vault.getBalances()

            # Format and return balance information
            lines = ["Vault Balance:"]
            lines.append("-" * 40)

            # SATORI balance
            if hasattr(vault, 'balance') and vault.balance:
                satori_amount = vault.balance.amount
                lines.append(f"  SATORI: {satori_amount:.8f}")
            else:
                lines.append("  SATORI: 0.00000000")

            # EVR balance
            if hasattr(vault, 'currency') and vault.currency:
                evr_amount = vault.currency.amount
                lines.append(f"  EVR:    {evr_amount:.8f}")
            else:
                lines.append("  EVR:    0.00000000")

            return "\n".join(lines)

        except Exception as e:
            return f"Error fetching vault balance: {e}"

    def handle_command(self, user_input: str) -> str | None:
        """Process user commands and return response."""
        user_input = user_input.strip()

        if user_input == "/help":
            return """Satori Neuron CLI - Available Commands

Wallet & Vault:
  /balance       - Show wallet or vault balance (interactive menu)
  /vault-status  - Show vault status

System:
  /clear         - Clear the screen
  /help          - Show this help message
  /exit          - Exit CLI"""

        elif user_input == "/balance":
            # Check if wallet manager exists
            if not self.wallet_manager:
                return "Wallet manager not initialized."

            # Interactive menu for balance
            options = [
                {'label': 'Wallet Balance', 'action': 'wallet'},
                {'label': 'Vault Balance', 'action': 'vault'}
            ]

            selected = self.interactive_menu("Balance Menu", options)

            if selected is None:
                return "Cancelled."

            # Execute the selected option
            action = options[selected]['action']
            if action == 'wallet':
                return self.get_wallet_balance_electrumx()
            elif action == 'vault':
                return self.get_vault_balance_electrumx()

        elif user_input == "/vault-status":
            if not self.wallet_manager:
                return "Wallet manager not initialized."

            vault = self.wallet_manager.vault
            if vault is None:
                return "Vault: Not created"

            status_lines = [
                f"Vault Address: {vault.address}",
                f"Decrypted: {vault.isDecrypted if hasattr(vault, 'isDecrypted') else 'Unknown'}",
            ]
            return "\n".join(status_lines)

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


    def show_main_menu(self) -> str | None:
        """Show interactive main menu and return selected action."""
        # Clear screen and show logo
        console_write("\033[2J\033[H")
        console_print()
        console_print("           @@@@")
        console_print("      @@@@@@@@@@@@@@@@@")
        console_print("    @@@@@@@@@@@@@@@@@@@@@@@")
        console_print("  @@@@@@@@      @@@@@  @@@@@@")
        console_print(" @@@@@@@           @@@ @@@@@@")
        console_print("@@@@@@               @@@@@@@@@")
        console_print("@@@@@                  @@@@@@@")
        console_print("@@@@                    @@@@@@")
        console_print("@@@@                     @@@@@")
        console_print("@@@@           @         @@@@@")
        console_print("@@@@         @@@@@       @@@@@")
        console_print("@@@@@        @@@@@      @@@@@@")
        console_print(" @@@@@    @@@@@@@@@    @@@@@")
        console_print("  @@@@@@  @@@@@@@@@   @@@@@")
        console_print("   @@@@@@ @@@@@@@@@ @@@@@@")
        console_print("     @@@@@@@@@@@@@@@@@@@@")
        console_print("        @@@@@@@@@@@@@@")
        console_print()
        console_print("    Satori Neuron CLI")
        console_print()

        options = [
            {'label': 'Check Balance', 'action': 'balance'},
            {'label': 'Vault Status', 'action': 'vault-status'},
            {'label': 'Help', 'action': 'help'},
            {'label': 'Clear Screen', 'action': 'clear'},
            {'label': 'Exit', 'action': 'exit'}
        ]

        selected = self.interactive_menu("Main Menu", options)

        if selected is None:
            return None

        return options[selected]['action']

    def run(self):
        """Run interactive CLI loop."""
        console_print()
        console_print("           @@@@")
        console_print("      @@@@@@@@@@@@@@@@@")
        console_print("    @@@@@@@@@@@@@@@@@@@@@@@")
        console_print("  @@@@@@@@      @@@@@  @@@@@@")
        console_print(" @@@@@@@           @@@ @@@@@@")
        console_print("@@@@@@               @@@@@@@@@")
        console_print("@@@@@                  @@@@@@@")
        console_print("@@@@                    @@@@@@")
        console_print("@@@@                     @@@@@")
        console_print("@@@@           @         @@@@@")
        console_print("@@@@         @@@@@       @@@@@")
        console_print("@@@@@        @@@@@      @@@@@@")
        console_print(" @@@@@    @@@@@@@@@    @@@@@")
        console_print("  @@@@@@  @@@@@@@@@   @@@@@")
        console_print("   @@@@@@ @@@@@@@@@ @@@@@@")
        console_print("     @@@@@@@@@@@@@@@@@@@@")
        console_print("        @@@@@@@@@@@@@@")
        console_print()
        console_print("    Satori Neuron CLI")
        console_print("    Wallet & Vault Manager")
        console_print()

        # Prompt for vault setup or unlock (matches web UI behavior)
        try:
            if not self.prompt_vault_setup_or_unlock():
                console_print("Vault access required. Exiting.")
                return
        except (KeyboardInterrupt, EOFError):
            console_print()
            console_print("Exiting...")
            return

        # Initialize wallet manager after vault is unlocked
        try:
            from satorineuron.init.wallet import WalletManager
            self.wallet_manager = WalletManager.create(useConfigPassword=False)

            # Unlock vault with stored password
            if self._vault_password:
                vault = self.wallet_manager.openVault(password=self._vault_password)
                if vault and hasattr(vault, 'isDecrypted') and vault.isDecrypted:
                    console_print()  # Just add blank line after vault unlock
                else:
                    console_print("Warning: Could not unlock vault in wallet manager.")
                    console_print()
        except Exception as e:
            console_print(f"Error initializing wallet manager: {e}")
            console_print("Exiting.")
            return

        while True:
            try:
                # Show interactive main menu
                action = self.show_main_menu()

                if action is None:
                    continue

                if action == 'exit':
                    console_print("Goodbye!")
                    break

                # Execute the selected action
                if action == 'balance':
                    response = self.handle_command("/balance")
                elif action == 'vault-status':
                    response = self.handle_command("/vault-status")
                elif action == 'help':
                    response = self.handle_command("/help")
                elif action == 'clear':
                    response = self.handle_command("/clear")
                    continue  # Don't print response for clear
                else:
                    response = "Unknown action"

                if response:
                    console_print(response)
                    console_print()
                    console_print("Press Enter to continue...")
                    console_readline()

            except KeyboardInterrupt:
                console_print("\nGoodbye!")
                break
            except EOFError:
                console_print("\nGoodbye!")
                break


if __name__ == "__main__":
    cli = NeuronCLI(env='prod', runMode='worker')
    cli.run()
