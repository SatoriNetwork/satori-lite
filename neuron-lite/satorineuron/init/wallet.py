from typing import Union, Optional
import os
import shutil
import yaml
from satorilib import logging
from satorilib.wallet.evrmore.wallet import EvrmoreWallet
from satorilib.wallet.evrmore.identity import EvrmoreIdentity
from satorilib.wallet.link import linkIsFor, makeLink
from satorilib.electrumx.electrumx import Electrumx
from satorineuron import config


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
    def create() -> 'WalletManager':
        """
        The install's keys: the alias, made if missing, and the wallet when
        its file exists, still locked (openWallet decrypts it, or makes it
        at wallet setup).
        """
        aliasPath, walletPath = config.aliasPath(), config.walletPath()
        WalletManager.performMigrationBackup(aliasPath)
        WalletManager.performMigrationBackup(walletPath)
        manager = WalletManager()
        manager._walletPath = walletPath
        manager._initializeAlias(aliasPath)
        manager._initializeWallet(walletPath)
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
        self._walletPath: Optional[str] = None

    def _initializeAlias(self, aliasPath: str):
        """Initialize the alias, which doesn't require a password (made if missing)."""
        self._alias = EvrmoreWallet.create(
            walletPath=aliasPath,
            identity=EvrmoreIdentity(aliasPath),
            persistent=False,  # We want lazy connection
        )
        logging.info('initialized alias')

    def _initializeWallet(
        self,
        walletPath: str,
        password: Union[str, None] = None,
        create: bool = False,
    ) -> Union[EvrmoreWallet, None]:
        """
        Initialize the wallet based on different scenarios:
        1. No wallet file exists and no password: Return None (wait for password)
        2. No wallet file exists and have password: Create new wallet
        3. Wallet file exists: Try to load it (will be encrypted if password not provided)
        Called only while there is no wallet yet (create, and openWallet before one).
        """
        try:
            # Case 1: No wallet file and no password - wait for password
            if not os.path.exists(walletPath) and not create:
                return None

            # Cases 2 and 3: create a new wallet or load the existing one
            self._wallet = EvrmoreWallet.create(
                walletPath=walletPath,
                identity=EvrmoreIdentity(walletPath, password=password),
                persistent=False,  # We want lazy connection
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
        self.ensureOnchainLink()
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

    @property
    def onchainAlias(self):
        """The alias for the Satori contract (stake with, the daily batch):
        onchain-alias.yaml's when there is one (init/onchain_alias.py), else
        the alias."""
        from satorineuron.init.onchain_alias import readOnchainAlias
        try:
            return readOnchainAlias() or self._alias
        except Exception as e:
            logging.warning(f'could not read the on-chain alias, using the alias: {e}')
            return self._alias

    @property
    def onchainLink(self) -> Optional[dict]:
        """The wallet's stored link to the on-chain alias, or None."""
        from satorineuron.init.onchain_alias import onchainLinkPath
        return readWalletLink(onchainLinkPath())

    def ensureOnchainLink(self) -> Optional[dict]:
        """With the wallet decrypted and another alias for the Satori
        contract, make sure the wallet's link to it is stored. Returns the
        link, or None (no such alias, or the wallet locked)."""
        from satorineuron.init.onchain_alias import readOnchainAlias, onchainLinkPath
        wallet = self._wallet
        if not isinstance(wallet, EvrmoreWallet) or not wallet.isDecrypted:
            return None
        try:
            onchain = readOnchainAlias()
            if onchain is None:
                return None
            link = readWalletLink(onchainLinkPath())
            if linkIsFor(link, onchain.ethAddress, wallet.ethAddress):
                return link
            link = makeLink(wallet, onchain.ethAddress)
            writeWalletLink(link, onchainLinkPath())
            logging.info('the wallet linked the on-chain alias')
            return link
        except Exception as e:
            logging.warning(f'could not make the wallet\'s link to the on-chain alias: {e}')
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
                    hostPorts=config.get().get('electrumx servers'))

            if self._electrumx and self.isConnected():
                # Update the alias and the wallet with current electrumx instance
                if self._alias:
                    self._alias.electrumx = self._electrumx
                if self._wallet:
                    self._wallet.electrumx = self._electrumx
                return True
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

        return self._initializeWallet(self._walletPath, password=password, create=create)

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
