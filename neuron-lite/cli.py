#!/usr/bin/env python3
"""
Satori Neuron CLI - Interactive command-line interface for the wallet and the alias.
Provides commands to check balances and manage the wallet.
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
            elif ch == '\x1b':  # Escape sequence (arrow keys or Esc)
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
                else:
                    # Esc key pressed (not followed by '[')
                    console_write('\r\n')
                    return None  # Return None to indicate cancellation
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


def console_password_input(prompt: str = "") -> str:
    """Read password input with asterisk masking."""
    console_write(prompt)

    # Use the original stdin fd for reading
    fd = _original_stdin_fd
    old_settings = termios.tcgetattr(fd)

    try:
        tty.setraw(fd)

        password = ""

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
                if len(password) > 0:
                    password = password[:-1]
                    # Erase last asterisk
                    console_write('\b \b')
            elif ch >= ' ':  # Printable character
                password += ch
                console_write('*')

        return password

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


class NeuronCLI:
    """Interactive CLI for Satori Neuron."""

    def __init__(self, env: str = 'prod', runMode: str = 'worker'):
        self.env = env
        self.runMode = runMode
        self.wallet_manager = None
        self._wallet_password = None  # Store password in memory for session

    def check_wallet_file_exists(self) -> bool:
        """Check if the wallet's file exists (like web UI does)."""
        try:
            from satorineuron import config
            return os.path.exists(config.walletPath())
        except Exception:
            return False

    def check_wallet_password_exists(self) -> bool:
        """Check if wallet password exists in config."""
        from satorineuron import config
        wallet_password = config.walletPassword()
        return wallet_password is not None and len(str(wallet_password)) > 0

    def prompt_mandatory_wallet_password(self) -> bool:
        """Prompt user to create a wallet password. Returns True if successful."""
        # Check if wallet password already exists (could be set via web UI or another CLI instance)
        if self.check_wallet_password_exists():
            console_print()
            console_print("=" * 60)
            console_print("  WALLET PASSWORD ALREADY EXISTS")
            console_print("=" * 60)
            console_print()
            console_print("A wallet password has already been created.")
            console_print("You can use it to unlock your wallet.")
            console_print()
            console_print("If you set it in another interface (web UI), use that password.")
            console_print("=" * 60)
            console_print()
            return True

        console_print()
        console_print("=" * 60)
        console_print("  WALLET PASSWORD SETUP REQUIRED")
        console_print("=" * 60)
        console_print()
        console_print("A wallet password is required to secure your wallet.")
        console_print("This password encrypts your private keys and funds.")
        console_print()
        console_print("IMPORTANT: Save this password in a secure location!")
        console_print("If you lose this password, you will lose access to your wallet.")
        console_print("There is no way to recover a lost wallet password.")
        console_print()
        console_print("=" * 60)
        console_print()

        while True:
            password1 = console_password_input("Enter new wallet password (min 4 characters): ")

            if len(password1) < 4:
                console_print("Password must be at least 4 characters. Please try again.")
                console_print()
                continue

            password2 = console_password_input("Confirm password: ")

            if password1 != password2:
                console_print("Passwords do not match. Please try again.")
                console_print()
                continue

            # Double-check if password was created elsewhere (race condition protection)
            if self.check_wallet_password_exists():
                console_print()
                console_print("Wallet password was already created by another process.")
                console_print("Please use the existing password to unlock your wallet.")
                console_print()
                return True

            # Save password to config
            from satorineuron import config
            config.add(data={'wallet password': password1})
            self._wallet_password_set = True

            console_print()
            console_print("Wallet password saved successfully!")
            console_print()
            console_print("REMINDER: Please save your password securely!")
            console_print("         You will need it to access your wallet.")
            console_print()
            return True

    def prompt_wallet_setup_or_unlock(self) -> bool:
        """
        Prompt user to either create new wallet or unlock existing wallet.
        Returns True if successful, False otherwise.
        This matches the web UI behavior - passwords are NOT saved to config.
        """
        from satorineuron import config

        # Check if config password exists (backward compatibility)
        config_password = config.walletPassword()

        if not self.check_wallet_file_exists():
            # No wallet file - need to create new wallet
            console_print()
            console_print("=" * 60)
            console_print("  WALLET SETUP")
            console_print("=" * 60)
            console_print()
            console_print("No wallet found. Let's create a new one.")
            console_print()
            console_print("Create a secure password to protect your wallet.")
            console_print("This password encrypts your private keys and funds.")
            console_print()
            console_print("IMPORTANT: Save this password in a secure location!")
            console_print("If you lose this password, you will lose access to your wallet.")
            console_print("There is no way to recover a lost wallet password.")
            console_print()
            console_print("=" * 60)
            console_print()

            return self.setup_new_wallet()
        else:
            # Wallet file exists - need to unlock it
            console_print()
            console_print("=" * 60)
            console_print("  WALLET UNLOCK")
            console_print("=" * 60)
            console_print()

            # Try config password first (backward compatibility, like web UI)
            if config_password:
                console_print("Attempting to unlock wallet with config password...")
                if self.unlock_existing_wallet(config_password):
                    console_print("Wallet unlocked successfully!")
                    console_print()
                    return True
                else:
                    console_print("Config password failed. Please enter your password manually.")
                    console_print()

            console_print("Please enter your wallet password to unlock.")
            console_print("(Press Ctrl+C to exit)")
            console_print()
            console_print("=" * 60)
            console_print()

            # Prompt for password (unlimited attempts, like web UI)
            while True:
                password = console_password_input("Enter wallet password: ")

                if self.unlock_existing_wallet(password):
                    console_print()
                    console_print("Wallet unlocked successfully!")
                    console_print()
                    return True
                else:
                    console_print("Invalid password. Please try again.")
                    console_print()

    def setup_new_wallet(self) -> bool:
        """
        Create a new wallet with password (DO NOT save password to config).
        Matches web UI behavior.
        """
        while True:
            password1 = console_password_input("Enter new wallet password (min 4 characters): ")

            if len(password1) < 4:
                console_print("Password must be at least 4 characters. Please try again.")
                console_print()
                continue

            password2 = console_password_input("Confirm password: ")

            if password1 != password2:
                console_print("Passwords do not match. Please try again.")
                console_print()
                continue

            # Try to create the wallet using WalletManager
            try:
                from satorineuron.init.wallet import WalletManager

                # Create wallet manager without using config password
                wallet_manager = WalletManager.create(useConfigPassword=False)
                wallet = wallet_manager.openWallet(password=password1, create=True)

                if wallet and wallet.isDecrypted:
                    self._wallet_password = password1  # Store in memory for session
                    console_print()
                    console_print("Wallet created successfully!")
                    console_print()
                    console_print("REMINDER: Please save your password securely!")
                    console_print("         You will need it to access your wallet.")
                    console_print()
                    return True
                else:
                    console_print("Error creating wallet. Please try again.")
                    console_print()
                    return False
            except Exception as e:
                console_print(f"Error creating wallet: {e}")
                console_print()
                return False

    def unlock_existing_wallet(self, password: str) -> bool:
        """
        Unlock existing wallet with password.
        Returns True if successful, False otherwise.
        """
        try:
            from satorineuron.init.wallet import WalletManager

            # Create wallet manager without using config password
            wallet_manager = WalletManager.create(useConfigPassword=False)
            wallet = wallet_manager.openWallet(password=password, create=False)

            if wallet and wallet.isDecrypted:
                self._wallet_password = password  # Store in memory for session
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
                            # Move cursor back to start of menu and clear all lines atomically
                            console_write(f'\x1b[{lines_printed}A\r')

                            # Redraw menu - each line starts with carriage return to ensure leftmost position
                            console_write(f'\x1b[2K{title}\r\n')
                            console_write(f'\x1b[2K' + "-" * len(title) + '\r\n')
                            for i, option in enumerate(options):
                                if i == selected:
                                    console_write(f'\x1b[2K  > \x1b[7m{option["label"]}\x1b[0m\r\n')
                                else:
                                    console_write(f'\x1b[2K    {option["label"]}\r\n')
                            console_write('\x1b[2K\r\n')  # Clear blank line
                            console_write('\x1b[2K(Use ↑/↓ arrows to navigate, Enter to select, Esc to cancel)\r\n')

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

    def get_alias_balance_electrumx(self) -> str:
        """Fetch alias balance from ElectrumX servers."""
        if not self.wallet_manager:
            return "Wallet manager not initialized."

        console_print("Fetching alias balance...")
        console_print()

        try:
            # Ensure ElectrumX connection with retry
            if hasattr(self.wallet_manager, 'connect'):
                for _ in range(3):
                    if self.wallet_manager.connect():
                        break
                    time.sleep(1)

            # Get alias
            alias = self.wallet_manager.alias
            if not alias:
                return "Alias not available."

            # Fetch balances from ElectrumX (retry if connection not ready)
            if hasattr(alias, 'getBalances'):
                for _ in range(3):
                    if alias.electrumx and alias.electrumx.connected():
                        alias.getBalances()
                        break
                    time.sleep(1)
                else:
                    # Final attempt even if connection check failed
                    alias.getBalances()

            # Format and return balance information
            lines = ["Alias Balance:"]
            lines.append("-" * 40)

            # SATORIEVR balance
            if hasattr(alias, 'balance') and alias.balance:
                satori_amount = alias.balance.amount
                lines.append(f"  SATORIEVR: {satori_amount:.8f}")
            else:
                lines.append("  SATORIEVR: 0.00000000")

            # EVR balance
            if hasattr(alias, 'currency') and alias.currency:
                evr_amount = alias.currency.amount
                lines.append(f"  EVR:       {evr_amount:.8f}")
            else:
                lines.append("  EVR:       0.00000000")

            lines.append("")
            lines.append("Note: SATORIEVR is the official Satori token (migrated from SATORI).")

            return "\n".join(lines)

        except Exception as e:
            return f"Error fetching alias balance: {e}"

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

            # Re-open wallet with stored password to ensure it's decrypted
            if self._wallet_password:
                wallet = self.wallet_manager.openWallet(password=self._wallet_password)
            else:
                wallet = self.wallet_manager.wallet

            if not wallet:
                return "Wallet not available.\nPlease create or unlock your wallet first."

            # Check if wallet is decrypted
            if hasattr(wallet, 'isDecrypted') and not wallet.isDecrypted:
                return "Wallet is locked.\nPlease unlock your wallet first."

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

            # SATORIEVR balance
            if hasattr(wallet, 'balance') and wallet.balance:
                satori_amount = wallet.balance.amount
                lines.append(f"  SATORIEVR: {satori_amount:.8f}")
            else:
                lines.append("  SATORIEVR: 0.00000000")

            # EVR balance
            if hasattr(wallet, 'currency') and wallet.currency:
                evr_amount = wallet.currency.amount
                lines.append(f"  EVR:       {evr_amount:.8f}")
            else:
                lines.append("  EVR:       0.00000000")

            lines.append("")
            lines.append("Note: SATORIEVR is the official Satori token (migrated from SATORI).")

            return "\n".join(lines)

        except Exception as e:
            return f"Error fetching wallet balance: {e}"

    def send_transaction_alias(self) -> str:
        """Send SATORI transaction from alias."""
        if not self.wallet_manager:
            return "Wallet manager not initialized."

        try:
            # Ensure ElectrumX connection
            if hasattr(self.wallet_manager, 'connect'):
                for _ in range(3):
                    if self.wallet_manager.connect():
                        break
                    time.sleep(1)

            # Get alias
            alias = self.wallet_manager.alias
            if not alias:
                return "Alias not available."

            # Fetch current balance
            console_print("Fetching current balance...")
            if hasattr(alias, 'getBalances'):
                alias.getBalances()

            console_print()
            console_print("Send Transaction from Alias                    [Esc to cancel]")
            console_print("=" * 60)
            console_print()
            console_print("Current Balance:")
            console_print(f"  SATORIEVR: {alias.balance.amount:.8f}")
            console_print(f"  EVR:       {alias.currency.amount:.8f}")
            console_print()

            # Get recipient address
            address = console_input("Enter recipient address: ")
            if address is None:
                return "Transaction cancelled. Returning to main menu."
            address = address.strip()
            if not address:
                return "Transaction cancelled. Returning to main menu."

            # Validate address
            from satorilib.wallet.utils.validate import Validate
            if not Validate.address(address, alias.symbol):
                return f"Invalid address: {address}\nReturning to main menu."

            # Clear screen and show amount prompt
            console_write("\033[2J\033[H")
            console_print()
            console_print("Send Transaction from Alias                    [Esc to cancel]")
            console_print("=" * 60)
            console_print(f"Recipient: {address}")
            console_print()
            console_print("Current Balance:")
            console_print(f"  SATORIEVR: {alias.balance.amount:.8f}")
            console_print(f"  EVR:       {alias.currency.amount:.8f}")
            console_print()

            # Get amount
            console_print("Note: SATORIEVR is the official Satori token (migrated from SATORI).")
            amount_str = console_input("Enter amount of SATORIEVR to send: ")
            if amount_str is None:
                return "Transaction cancelled. Returning to main menu."
            amount_str = amount_str.strip()
            if not amount_str:
                return "Transaction cancelled. Returning to main menu."

            try:
                amount = float(amount_str)
            except ValueError:
                return f"Invalid amount: {amount_str}\nReturning to main menu."

            if amount <= 0:
                return "Amount must be greater than 0.\nReturning to main menu."

            if amount > alias.balance.amount:
                return f"Insufficient balance. You have {alias.balance.amount:.8f} SATORIEVR\nReturning to main menu."

            # Show confirmation
            console_print()
            console_print("Transaction Confirmation                        [Esc to cancel]")
            console_print("=" * 60)
            console_print(f"  From:   Alias")
            console_print(f"  To:     {address}")
            console_print(f"  Amount: {amount} SATORIEVR")
            console_print("=" * 60)
            console_print()

            confirm = console_input("Type 'yes' to confirm: ")
            if confirm is None:
                return "Transaction cancelled. Returning to main menu."
            confirm = confirm.strip().lower()
            if confirm not in ['yes', 'y']:
                return "Transaction cancelled. Returning to main menu."

            # Prepare for transaction
            console_print()
            console_print("Preparing transaction...")
            alias.getReadyToSend(balance=True, save=True)

            # Send transaction
            console_print("Sending transaction...")
            txid = alias.satoriTransaction(amount=amount, address=address)

            return f"\nTransaction successful!\nTransaction ID: {txid}"

        except Exception as e:
            return f"Error sending transaction: {e}"

    def send_transaction_wallet(self) -> str:
        """Send SATORI transaction from wallet."""
        if not self.wallet_manager:
            return "Wallet manager not initialized."

        try:
            # Ensure ElectrumX connection
            if hasattr(self.wallet_manager, 'connect'):
                for _ in range(3):
                    if self.wallet_manager.connect():
                        break
                    time.sleep(1)

            # Get wallet
            if self._wallet_password:
                wallet = self.wallet_manager.openWallet(password=self._wallet_password)
            else:
                wallet = self.wallet_manager.wallet

            if not wallet:
                return "Wallet not available.\nPlease create or unlock your wallet first."

            if hasattr(wallet, 'isDecrypted') and not wallet.isDecrypted:
                return "Wallet is locked.\nPlease unlock your wallet first."

            # Fetch current balance
            console_print("Fetching current balance...")
            if hasattr(wallet, 'getBalances'):
                wallet.getBalances()

            console_print()
            console_print("Send Transaction from Wallet                     [Esc to cancel]")
            console_print("=" * 60)
            console_print()
            console_print("Current Balance:")
            console_print(f"  SATORIEVR: {wallet.balance.amount:.8f}")
            console_print(f"  EVR:       {wallet.currency.amount:.8f}")
            console_print()

            # Get recipient address
            address = console_input("Enter recipient address: ")
            if address is None:
                return "Transaction cancelled. Returning to main menu."
            address = address.strip()
            if not address:
                return "Transaction cancelled. Returning to main menu."

            # Validate address
            from satorilib.wallet.utils.validate import Validate
            if not Validate.address(address, wallet.symbol):
                return f"Invalid address: {address}\nReturning to main menu."

            # Clear screen and show amount prompt
            console_write("\033[2J\033[H")
            console_print()
            console_print("Send Transaction from Wallet                     [Esc to cancel]")
            console_print("=" * 60)
            console_print(f"Recipient: {address}")
            console_print()
            console_print("Current Balance:")
            console_print(f"  SATORIEVR: {wallet.balance.amount:.8f}")
            console_print(f"  EVR:       {wallet.currency.amount:.8f}")
            console_print()

            # Get amount
            console_print("Note: SATORIEVR is the official Satori token (migrated from SATORI).")
            amount_str = console_input("Enter amount of SATORIEVR to send: ")
            if amount_str is None:
                return "Transaction cancelled. Returning to main menu."
            amount_str = amount_str.strip()
            if not amount_str:
                return "Transaction cancelled. Returning to main menu."

            try:
                amount = float(amount_str)
            except ValueError:
                return f"Invalid amount: {amount_str}\nReturning to main menu."

            if amount <= 0:
                return "Amount must be greater than 0.\nReturning to main menu."

            if amount > wallet.balance.amount:
                return f"Insufficient balance. You have {wallet.balance.amount:.8f} SATORIEVR\nReturning to main menu."

            # Show confirmation
            console_print()
            console_print("Transaction Confirmation                        [Esc to cancel]")
            console_print("=" * 60)
            console_print(f"  From:   Wallet")
            console_print(f"  To:     {address}")
            console_print(f"  Amount: {amount} SATORIEVR")
            console_print("=" * 60)
            console_print()

            confirm = console_input("Type 'yes' to confirm: ")
            if confirm is None:
                return "Transaction cancelled. Returning to main menu."
            confirm = confirm.strip().lower()
            if confirm not in ['yes', 'y']:
                return "Transaction cancelled. Returning to main menu."

            # Prepare for transaction
            console_print()
            console_print("Preparing transaction...")
            wallet.getReadyToSend(balance=True, save=True)

            # Send transaction
            console_print("Sending transaction...")
            txid = wallet.satoriTransaction(amount=amount, address=address)

            return f"\nTransaction successful!\nTransaction ID: {txid}"

        except Exception as e:
            return f"Error sending transaction: {e}"

    def handle_command(self, user_input: str) -> str | None:
        """Process user commands and return response."""
        user_input = user_input.strip()

        if user_input == "/help":
            return """Satori Neuron CLI - Available Commands

Wallet & Alias:
  /balance       - Show the wallet's or the alias's balance (interactive menu)
  /wallet-status - Show wallet status

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
                {'label': 'Alias Balance', 'action': 'alias'},
                {'label': 'Wallet Balance', 'action': 'wallet'},
                {'label': 'Back to Main Menu', 'action': 'back'}
            ]

            selected = self.interactive_menu("Balance Menu", options)

            if selected is None:
                return "Cancelled."

            # Execute the selected option
            action = options[selected]['action']
            if action == 'back':
                return None
            elif action == 'alias':
                return self.get_alias_balance_electrumx()
            elif action == 'wallet':
                return self.get_wallet_balance_electrumx()

        elif user_input == "/wallet-status":
            if not self.wallet_manager:
                return "Wallet manager not initialized."

            wallet = self.wallet_manager.wallet
            if wallet is None:
                return "Wallet: Not created"

            status_lines = [
                f"Wallet Address: {wallet.address}",
                f"Decrypted: {wallet.isDecrypted if hasattr(wallet, 'isDecrypted') else 'Unknown'}",
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
        from satorineuron import VERSION
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
        console_print(f"    Satori Neuron CLI - {VERSION}")
        console_print()

        options = [
            {'label': 'Check Balance', 'action': 'balance'},
            {'label': 'Send Transaction', 'action': 'send'},
            {'label': 'Wallet Status', 'action': 'wallet-status'},
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
        from satorineuron import VERSION
        console_print()
        console_print("    Satori Neuron CLI")
        console_print(f"    Wallet Manager - {VERSION}")
        console_print()

        # Prompt for wallet setup or unlock (matches web UI behavior)
        try:
            if not self.prompt_wallet_setup_or_unlock():
                console_print("Wallet access required. Exiting.")
                return
        except (KeyboardInterrupt, EOFError):
            console_print()
            console_print("Exiting...")
            return

        # Initialize wallet manager after wallet is unlocked
        try:
            from satorineuron.init.wallet import WalletManager
            self.wallet_manager = WalletManager.create(useConfigPassword=False)

            # Unlock wallet with stored password
            if self._wallet_password:
                wallet = self.wallet_manager.openWallet(password=self._wallet_password)
                if wallet and hasattr(wallet, 'isDecrypted') and wallet.isDecrypted:
                    console_print()  # Just add blank line after wallet unlock
                else:
                    console_print("Warning: Could not unlock wallet in wallet manager.")
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
                elif action == 'send':
                    # Show submenu for alias vs wallet
                    send_options = [
                        {'label': 'From Alias', 'action': 'alias'},
                        {'label': 'From Wallet', 'action': 'wallet'},
                        {'label': 'Back to Main Menu', 'action': 'back'}
                    ]
                    send_selected = self.interactive_menu("Send Transaction", send_options)

                    if send_selected is None:
                        continue

                    send_action = send_options[send_selected]['action']
                    if send_action == 'back':
                        continue
                    elif send_action == 'alias':
                        response = self.send_transaction_alias()
                    elif send_action == 'wallet':
                        response = self.send_transaction_wallet()
                    else:
                        response = "Unknown send action"
                elif action == 'wallet-status':
                    response = self.handle_command("/wallet-status")
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
    cli = NeuronCLI(env=os.environ.get('SATORI_ENV', 'prod'), runMode='worker')
    cli.run()
