from typing import Union, Optional, Callable
import os
import shutil
import yaml
from satorilib import logging
from satorilib.wallet.evrmore.wallet import EvrmoreWallet
from satorilib.wallet.evrmore.identity import EvrmoreIdentity
from satorilib.wallet.link import linkIsFor, makeLink
from satorilib.electrumx.electrumx import Electrumx
from satorineuron import config
from satorineuron.common.structs import ConnectionTo


class WalletManager:
    """
    The neuron's two keys.

    The alias is the automated signing key: made at startup with no input,
    kept unencrypted, it acts for the wallet with the Satori Network. The
    wallet is the person's own key that holds the funds, encrypted with their
    password, made at wallet setup. Which files hold them is
    config.keyFiles()'s answer.
    """

    @staticmethod
    def create(
        aliasPath: Optional[str] = None,
        walletPath: Optional[str] = None,
        walletPassword: Optional[str] = None,
        cachePath: Optional[str] = None,
        peersCache: Optional[str] = None,
        updateConnectionStatus: Optional[Callable] = None,
        useConfigPassword: bool = True,
        persistent: bool = False,
    ) -> 'WalletManager':
        """
        Create a new WalletManager with the alias and, when it exists or a
        password is given, the wallet.

        Args:
            aliasPath: Path to the alias's file (default: config.aliasPath())
            walletPath: Path to the wallet's file (default: config.walletPath())
            walletPassword: Password for the wallet
            cachePath: Path to cache directory
            peersCache: Path to peers cache file
            useConfigPassword: If True, read the wallet password from config if not provided
            persistent: If True, hold the Electrumx socket open across calls (for
                the long-lived wallet-service). Default False preserves the
                per-request reconnect behavior used by the web UI/CLI.
        """
        aliasPath = aliasPath or config.aliasPath()
        walletPath = walletPath or config.walletPath()
        if useConfigPassword:
            walletPassword = walletPassword or config.walletPassword()

        # Only create the wallet if we have a password and its file doesn't exist yet
        createWallet = walletPassword is not None and not os.path.exists(walletPath)

        WalletManager.performMigrationBackup(aliasPath)
        WalletManager.performMigrationBackup(walletPath)
        manager = WalletManager()
        manager.setup(aliasPath, walletPath, walletPassword, createWallet, cachePath, peersCache, updateConnectionStatus, persistent)
        return manager

    @staticmethod
    def performMigrationBackup(path: str):
        """Keep a one-time copy of a key file beside it (<file>.bak)."""
        if os.path.exists(path) and not os.path.exists(f'{path}.bak'):
            shutil.copy2(path, f'{path}.bak')

    def __init__(self):
        self._alias: Optional[EvrmoreWallet] = None
        self._wallet: Optional[EvrmoreWallet] = None
        self._electrumx: Optional[Electrumx] = None
        self._updateConnectionStatus: Optional[Callable] = None
        self._walletPath: Optional[str] = None
        self._cachePath: Optional[str] = None
        self._peersCache: Optional[str] = None
        self._persistent: bool = False

    def setup(
        self,
        aliasPath: str,
        walletPath: str,
        walletPassword: str,
        createWallet: bool,
        cachePath: Optional[str] = None,
        peersCache: Optional[str] = None,
        updateConnectionStatus: Optional[Callable] = None,
        persistent: bool = False,
    ):
        # Store paths for later use
        self._walletPath = walletPath
        self._cachePath = cachePath
        self._peersCache = peersCache
        self._updateConnectionStatus = updateConnectionStatus
        self._persistent = persistent

        self._initializeAlias(aliasPath, cachePath, peersCache)
        self._initializeWallet(walletPath, cachePath, peersCache, walletPassword, createWallet)

    def _initializeAlias(
        self,
        aliasPath: str,
        cachePath: Optional[str] = None,
        peersCache: Optional[str] = None,
    ) -> EvrmoreWallet:
        """Initialize the alias, which doesn't require a password (made if missing)."""
        if self._alias is not None:
            return self._alias
        self._alias = EvrmoreWallet.create(
            walletPath=aliasPath,
            cachePath=cachePath,
            identity=EvrmoreIdentity(aliasPath),
            persistent=False,  # We want lazy connection
            cachedPeersFile=peersCache
        )
        logging.info('initialized alias')
        return self._alias

    def _initializeWallet(
        self,
        walletPath: str,
        cachePath: Optional[str] = None,
        peersCache: Optional[str] = None,
        password: Union[str, None] = None,
        create: bool = False,
    ) -> Union[EvrmoreWallet, None]:
        """
        Initialize the wallet based on different scenarios:
        1. No wallet file exists and no password: Return None (wait for password)
        2. No wallet file exists and have password: Create new wallet
        3. Wallet file exists: Try to load it (will be encrypted if password not provided)
        """
        try:
            # Case 1: No wallet file and no password - wait for password
            if not os.path.exists(walletPath) and not create:
                return None

            # Case 2: Existing wallet instance - handle password updates
            if isinstance(self._wallet, EvrmoreWallet):
                if (
                    self._wallet.password is None
                    and isinstance(password, str)
                    and len(password) > 0
                ):
                    # Decrypt existing wallet with new password
                    self._wallet.open(password)
                    self.ensureWalletLink()
                    return self._wallet
                elif password is None or self._wallet.password == password:
                    # Return existing wallet if no password change needed
                    return self._wallet

            # Case 3: Create new wallet or load existing wallet
            self._wallet = EvrmoreWallet.create(
                walletPath=walletPath,
                identity=EvrmoreIdentity(walletPath, password=password),
                cachePath=cachePath,
                persistent=False,  # We want lazy connection
                cachedPeersFile=peersCache
            )
            logging.info('initialized wallet')
            self.ensureWalletLink()
            return self._wallet

        except Exception as e:
            logging.error(f'failed to open wallet: {str(e)}')
            raise e

    ### The wallet's link to the alias #########################################

    @property
    def walletLink(self) -> Optional[dict]:
        """The stored link ({'message', 'signature'}), or None before there is one."""
        return readWalletLink()

    def ensureWalletLink(self) -> Optional[dict]:
        """
        With the wallet decrypted, make sure a link from it to the alias is
        stored (making and saving one when there is none, or the stored one is
        for another alias or wallet). Returns the link, or None while the
        wallet is locked or missing.
        """
        wallet, alias = self._wallet, self._alias
        if not isinstance(wallet, EvrmoreWallet) or not wallet.isDecrypted or alias is None:
            return None
        try:
            link = readWalletLink()
            if linkIsFor(link, alias.ethAddress, wallet.ethAddress):
                return link
            link = makeLink(wallet, alias.ethAddress)
            writeWalletLink(link)
            logging.info('the wallet linked the alias')
            return link
        except Exception as e:
            logging.warning(f'could not make the wallet\'s link to the alias: {e}')
            return None

    def connect(self) -> bool:
        """
        Ensures there is a valid Electrumx connection, creating or reconnecting as needed.
        Returns True if connection is successful, False otherwise.
        """
        try:
            if not self.isConnected():
                if self._electrumx is not None:
                    try:
                        if not self._electrumx.reconnect():
                            self._electrumx = None
                    except Exception as e:
                        logging.debug(f"Failed to reconnect to Electrumx: {e}")
                        self._electrumx = None

            # Create new connection if needed
            if not self._electrumx:
                self._electrumx = Electrumx.create(
                    hostPorts=config.get().get('electrumx servers'),
                    persistent=self._persistent)

            if self._electrumx and self.isConnected():
                # Update the alias and the wallet with current electrumx instance
                if self._alias:
                    self._alias.electrumx = self._electrumx
                if self._wallet:
                    self._wallet.electrumx = self._electrumx
                if self._updateConnectionStatus:
                    self._updateConnectionStatus(
                        connTo=ConnectionTo.electrumx,
                        status=True)
                return True
            if self._updateConnectionStatus:
                self._updateConnectionStatus(
                    connTo=ConnectionTo.electrumx,
                    status=False)
            return False
        except Exception as e:
            logging.error(f"Failed to establish Electrumx connection: {e}")
            return False

    def isConnected(self) -> bool:
        """Check if there is a valid and active Electrumx connection."""
        return (
            self._electrumx is not None
            and self._electrumx.connected()
        )

    @property
    def alias(self) -> Optional[EvrmoreWallet]:
        """Get the alias without ensuring connection."""
        return self._alias

    @property
    def wallet(self) -> Optional[EvrmoreWallet]:
        """Get the wallet without ensuring connection."""
        return self._wallet

    def openWallet(
        self,
        password: Union[str, None] = None,
        create: bool = False,
    ) -> Union[EvrmoreWallet, None]:
        if isinstance(self._wallet, EvrmoreWallet):
            # SECURITY FIX: Always close and re-decrypt with provided password
            # This prevents bypassing password validation when the wallet is already decrypted
            self._wallet.close()
            self._wallet.open(password)
            self.ensureWalletLink()
            return self._wallet

        # Use stored wallet path, or default if not set
        walletPath = self._walletPath or config.walletPath()
        return self._initializeWallet(
            walletPath,
            self._cachePath,
            self._peersCache,
            password=password,
            create=create
        )

    def closeWallet(self) -> Union[EvrmoreWallet, None]:
        """Close the wallet, reopen it without decrypting it."""
        if isinstance(self._wallet, EvrmoreWallet):
            self._wallet.close()
        return self._wallet


def readWalletLink(path: Optional[str] = None) -> Optional[dict]:
    """The stored link ({'message', 'signature'}), or None."""
    path = path or config.walletLinkPath()
    try:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        if data.get('message') and data.get('signature'):
            return {'message': data['message'], 'signature': data['signature']}
    except Exception as e:
        logging.warning(f'could not read the wallet\'s link: {e}')
    return None


def writeWalletLink(link: dict, path: Optional[str] = None) -> None:
    """Store the link beside the keys (nothing in it is secret)."""
    path = path or config.walletLinkPath()
    with open(path, 'w') as f:
        yaml.safe_dump(
            {'message': link['message'], 'signature': link['signature']},
            f, default_flow_style=False, sort_keys=False)
