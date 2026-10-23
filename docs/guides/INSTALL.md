# Satori Lite Installation

## Prerequisites

- Docker installed and running
- macOS, Linux, or Windows (WSL2)

## Quick Install

```bash
curl -fsSL https://raw.githubusercontent.com/SatoriNetwork/neuron/main/install.sh | bash
```

This will:
1. Pull the Satori Lite Docker image
2. Install the `satori` command
3. Start the Satori neuron

## Manual Installation

### 1. Pull the Docker image

```bash
docker pull satorinet/neuron:latest
```

### 2. Start the neuron

```bash
docker run -d --name satori \
    -v satori-data:/Satori/Neuron/data \
    satorinet/neuron:latest
```

### 3. Install the CLI command

```bash
sudo curl -fsSL https://raw.githubusercontent.com/SatoriNetwork/neuron/main/satori -o /usr/local/bin/satori
sudo chmod +x /usr/local/bin/satori
```

## Usage

### Enter the CLI

```bash
satori
```

### CLI Commands

Once in the CLI, you can use:

| Command | Description |
|---------|-------------|
| `/status` | Show neuron status |
| `/balance` | Show the wallet's or the alias's balance |
| `/streams` | Show stream assignments |
| `/neuron-logs` | Show recent logs |
| `/pause` | Pause the engine |
| `/unpause` | Resume the engine |
| `/stake` | Check stake status |
| `/pool` | Show pool status |
| `/clear` | Clear the screen |
| `/help` | Show all commands |
| `/exit` | Exit CLI (neuron keeps running) |

### Exit the CLI

Type `/exit` or press `Ctrl+C` to exit the CLI. The neuron continues running in the background.

## Managing the Container

### Stop the neuron

```bash
docker stop satori
```

### Start the neuron

```bash
docker start satori
```

### View logs

```bash
docker logs satori
```

### Remove the neuron

```bash
docker stop satori
docker rm satori
```

### Remove all data

```bash
docker volume rm satori-data
```

## Updating

```bash
docker stop satori
docker rm satori
docker pull satorinet/neuron:latest
docker run -d --name satori \
    -v satori-data:/Satori/Neuron/data \
    satorinet/neuron:latest
```

## Troubleshooting

### Container not running

```bash
# Check if container exists
docker ps -a | grep satori

# Start if stopped
docker start satori

# Or recreate if missing
docker run -d --name satori \
    -v satori-data:/Satori/Neuron/data \
    satorinet/neuron:latest
```

### Cannot connect to CLI

```bash
# Check container is running
docker ps | grep satori

# Check container logs
docker logs satori --tail 50
```

### Reset everything

```bash
docker stop satori
docker rm satori
docker volume rm satori-data
# Then reinstall
```
