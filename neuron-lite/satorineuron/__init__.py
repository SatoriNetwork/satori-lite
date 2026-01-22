import os
from satorineuron import config
from satorilib import logging
from satorilib.disk import Cache

Cache.setConfig(config)

# Ensure logs directory exists
log_dir = config.root('logs')
os.makedirs(log_dir, exist_ok=True)

# Configure logging to write to file only
logging.setup(
    level={
        'debug': logging.DEBUG,
        'warning': logging.WARNING,
        'info': logging.INFO,
        'error': logging.ERROR,
        'critical': logging.CRITICAL,
    }.get(config.get().get('logging level', 'info').lower(), logging.INFO),
    file=config.root('logs/neuron.log'),
    stdoutAndFile=False
)

VERSION = 'v1.5'
MOTTO = 'Let your workings remain a mystery, just show people the results.'
