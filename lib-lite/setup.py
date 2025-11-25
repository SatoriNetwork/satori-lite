from setuptools import setup, find_packages

setup(
    name='satorilib-lite',
    version='0.1.0',
    description='Lightweight version of Satori library for minimal neuron deployment',
    author='Satori',
    packages=find_packages(),
    install_requires=[
        'pandas==1.5.2',
        'numpy==1.24.0',
        'pyarrow==10.0.1',
        'PyYAML==6.0',
        'psutil==5.9.0',
        'python-evrmorelib',
        'mnemonic==0.20',
        'base58==2.1.1',
        'pycryptodome==3.20.0',
        'eth-account==0.13.7',
        'eth-keys==0.6.1',
        'web3==7.12.1',
        'marshmallow==3.22.0',
        'requests>=2.31.0',
        'websockets==15.0.1',
        'cryptography==44.0.2',
        'ipaddress==1.0.23',
    ],
    extras_require={
        'centrifugo': ['centrifuge-python==0.4.1'],
        'telemetry': [
            'sanic>=23.6.0',
            'aiosqlite>=0.19.0',
            'uvloop>=0.19.0',
        ],
    },
    python_requires='>=3.8',
)
