# Quick Start Guide

## 1. Install Everything

```bash
cd /Users/krishna-mac/Desktop/Satori/repos/satori-lite

# Install all dependencies
pip install -r requirements.txt

# Install lib-lite package
cd lib-lite
pip install -e .
cd ..
```

## 2. Verify Installation

```bash
python3 test_imports.py
```

Expected output:
```
✓ Concepts imported successfully
✓ Constants imported successfully
✓ Wallet modules imported successfully
✓ Server modules imported successfully
✓ Utils imported successfully
✓ Asynchronous imported successfully
✓ Disk imported successfully
✅ All required imports successful!
```

## 3. (Optional) Enable Centrifugo

If you want real-time messaging support:

```bash
pip install centrifuge-python==0.4.1
```

## 4. Test Basic Imports in Python

```python
# Test in Python REPL
import sys
sys.path.insert(0, 'lib-lite')

from satorilib.concepts.structs import StreamId, Stream
from satorilib.wallet import EvrmoreWallet
from satorilib.server import SatoriServerClient

print("All imports work!")
```

## 5. What's Next?

You now have a working lib-lite installation. You can:

### A. Use it directly in Python
```python
from satorilib import StreamId, EvrmoreWallet, SatoriServerClient

# Create stream ID
stream = StreamId(source='binance', author='user', stream='btcusdt')

# Work with wallet
wallet = EvrmoreWallet()

# Connect to server
server = SatoriServerClient(wallet, url='https://satori.io')
```

### B. Run start.py (when ready)
```bash
cd neuron-lite
python3 start.py
```

## Common Issues

### Issue: "No module named 'pandas'"
**Solution**: Install dependencies first
```bash
pip install -r requirements.txt
```

### Issue: "No module named 'satorilib'"
**Solution**: Install lib-lite package
```bash
cd lib-lite
pip install -e .
```

### Issue: Python version error
**Solution**: Ensure Python 3.8+
```bash
python3 --version  # Should be 3.8 or higher
```

## Directory Reference

- **lib-lite/** - The minimal satorilib package
- **neuron-lite/** - Contains start.py
- **engine-lite/** - Empty (for future use)
- **requirements.txt** - All dependencies
- **README.md** - Full project documentation
- **INSTALL.md** - Detailed installation guide
- **COMPONENTS.md** - Component details

## Support

For detailed information, see:
- `INSTALL.md` - Comprehensive installation instructions
- `COMPONENTS.md` - What's included and why
- `SUMMARY.md` - Complete implementation summary
