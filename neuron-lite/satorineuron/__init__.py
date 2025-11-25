from satorineuron import config
from satorilib import logging
from satorilib.disk import Cache

Cache.setConfig(config)
logging.setup(level={
    'debug': logging.DEBUG,
    'warning': logging.WARNING,
    'info': logging.INFO,
    'error': logging.ERROR,
    'critical': logging.CRITICAL,
}.get(config.get().get('logging level', 'info').lower(), logging.INFO))

VERSION = '0.4.23'
MOTTO = 'Let your workings remain a mystery, just show people the results.'
