# Satori Neuron - Quick Start

## Basic Usage
```bash
./satori              # Start and enter interactive CLI (instance 1, port 24601)
```

## Commands
| Command | Description |
|---------|-------------|
| `./satori` | Enter interactive CLI (starts container if needed) |
| `./satori start` | Start the neuron in background |
| `./satori stop` | Stop and remove the container |
| `./satori restart` | Restart the neuron |
| `./satori logs` | View live logs |
| `./satori status` | Check if neuron is running |
| `./satori --help` | Show help |

## Running Multiple Instances
```bash
./satori 1            # Instance 1 on port 24601
./satori 2            # Instance 2 on port 24602
./satori 2 stop       # Stop instance 2
```

## Data Storage
Data persists in `./<instance>/` with subdirectories:
- `config/` - Configuration files
- `wallet/` - Wallet data
- `models/` - AI models
- `data/` - Engine data

## Web Interface
Access at `http://localhost:24601` (or 24602, 24603... for additional instances)
