<h1 align="center">Thermal Key</h1>

<p align="center">
  <strong>Open-source CLI tools for Avalon Mini 3 & Nano 3S Bitcoin miners</strong><br>
  No vendor app. No cloud.
</p>

<p align="center">
  <img src="thermal-key.png" alt="Thermal Key CLI" width="600">
</p>

<p align="center">
  <em>Control, monitor, and automate your Canaan Avalon Mini 3 / Nano 3S ASIC miner from any terminal.</em>
</p>

<p align="center">
  <a href="#quick-start"><img src="https://img.shields.io/badge/Quick_Start-blue?style=flat-square" alt="Quick Start"></a>
  <a href="#fleet-management"><img src="https://img.shields.io/badge/Fleet_Management-green?style=flat-square" alt="Fleet Management"></a>
  <img src="https://img.shields.io/badge/python-3.8+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.8+">
  <img src="https://img.shields.io/badge/license-MIT-yellow?style=flat-square" alt="MIT License">
  <img src="https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-lightgrey?style=flat-square" alt="Platform">
</p>

---

## Why Thermal Key?

<table>
<tr>
<td width="50%">

### Vendor App
- Cloud account required
- Phone only
- Single device at a time
- No scripting
- Forgot password? Contact support

</td>
<td width="50%">

### Thermal Key
- 100% local
- Any terminal, any platform
- Fleet management built-in
- Fully automatable
- Recover passwords yourself

</td>
</tr>
</table>

> **WARNING**: Miners can overheat or get damaged by wrong settings if misused. No responsibility for bricked devices, fires, or lost coins.

---

## Quick Start

> **You'll need:** Python 3.8+ and your miner's IP address (check your router or the miner's LCD screen)

```bash
git clone https://github.com/mars-llm/thermal-key.git
cd thermal-key

python3 thermal.py -H 192.168.1.100 status   # Check miner status
python3 thermal.py -H 192.168.1.100 watch    # Live monitoring
```

No external dependencies required.

---

## Features

<table>
<tr>
<td>

### Real-Time Monitoring

```
$ python3 thermal.py -H 192.168.1.100 status

  Avalon Mini3 @ 192.168.1.100
  DNA: 0201000046d3803b

  Hashrate   40.12 TH/s (max 40.88)
  Errors     2.1% reject, 0 HW
  Temp       63C (max 75C)
  Fan        3000 RPM (100%)
  Power      1215W in, 750W out
  Freq       492 MHz @ 2109 mV
  Uptime     2h 15m
```

</td>
</tr>
</table>

---

## Fleet Management

Control your entire mining operation from one terminal:

```
$ python3 thermal.py -H 192.168.1.100,192.168.1.101,192.168.1.102 status

HOST             HASHRATE   TEMP   FAN  POWER   M  UPTIME
----------------------------------------------------------------------
192.168.1.100      40.1 TH/s   63C  100%  1215W   M  2h 15m
192.168.1.101      39.8 TH/s   61C  100%  1210W   M  5h 30m
192.168.1.102      OFFLINE
```

```bash
# Use a hosts file (one IP per line)
python3 thermal.py -H miners.txt status       # Check all miners
python3 thermal.py -H miners.txt reboot       # Reboot entire fleet
python3 thermal.py -H miners.txt fan 100      # Max fans everywhere
python3 thermal.py -H miners.txt -j 20 status # 20 parallel connections
```

---

## Password Recovery

Locked out of the web UI? Recover your password locally with some luck.

```bash
# Dictionary attack
python3 password.py -d 192.168.1.100 -w wordlist.txt

# Dictionary with mutations (l33t speak, case variations)
python3 password.py -d 192.168.1.100 -w words.txt --rules

# Hybrid: wordlist + suffix bruteforce
python3 password.py -d 192.168.1.100 -w words.txt --hybrid 4

# Pattern attack (e.g., admin followed by 4 digits)
python3 password.py -d 192.168.1.100 --mask "admin?d?d?d?d"

# Full bruteforce
python3 password.py -d 192.168.1.100 --bruteforce --max-len 6
```

<table>
<tr>
<th>Performance</th>
<th>Mask Characters</th>
</tr>
<tr>
<td>

~17M passwords/sec on Apple M4
6-char alphanumeric ≈ 1 hour

</td>
<td>

`?l` lowercase &nbsp; `?u` uppercase &nbsp; `?d` digit
`?s` special &nbsp; `?a` all &nbsp; `?w` alphanumeric

</td>
</tr>
</table>

---

## Commands

<details open>
<summary><h3>thermal.py — Device Control</h3></summary>

```bash
# Status & Monitoring
thermal.py -H IP status          # Full status
thermal.py -H IP pools           # List pools
thermal.py -H IP watch           # Live monitoring

# Control
thermal.py -H IP reboot          # Reboot device
thermal.py -H IP fan 80          # Fan speed (15-100 or 'auto')
thermal.py -H IP freq 500        # Frequency in MHz
thermal.py -H IP mode 1          # 0=Heater, 1=Mining, 2=Night

# Pool Management
thermal.py -H IP switchpool 0    # Switch active pool
thermal.py -H IP enablepool 1    # Enable pool
thermal.py -H IP disablepool 2   # Disable pool

# Authentication
thermal.py -H IP auth PASSWORD   # Authenticate to web UI
thermal.py -H IP getauth         # Get hash for password recovery

# Advanced
thermal.py -H IP raw COMMAND     # Raw CGMiner API command
thermal.py -H IP ascset "0,..."  # Raw ascset command
thermal.py -H IP ascset-help     # List all ascset options
```

</details>

<details>
<summary><h3>ascset — Direct Hardware Control</h3></summary>

> **Warning:** Can brick your device

| Command | Format | Notes |
|---------|--------|-------|
| voltage | `0,voltage,2100` | 1700-2200 mV |
| frequency | `0,frequency,500:500:500:500` | 4 PLL values (MHz) |
| workmode | `0,workmode,set,1` | 0=Heater, 1=Mining, 2=Night |
| fan-spd | `0,fan-spd,80` | 15-100%, or -1 for auto |
| reboot | `0,reboot,1` | Reboot device |
| lcd | `0,lcd,4:MODE` | Display mode |
| password | `0,password,old,new` | Change web password |

Full list: `thermal.py -H IP ascset-help`

</details>

---

## Automation Examples

```bash
# Pool switching via cron
0 22 * * * python3 /path/to/thermal.py -H 192.168.1.100 switchpool 1
0 6  * * * python3 /path/to/thermal.py -H 192.168.1.100 switchpool 0

# Low hashrate alert
hashrate=$(python3 thermal.py -H 192.168.1.100 raw summary | jq -r '.SUMMARY[0]."MHS av"')
if (( $(echo "$hashrate < 30000000" | bc -l) )); then
  echo "Low hashrate!" | mail -s "Alert" you@email.com
fi

# Seasonal heating
python3 thermal.py -H 192.168.1.100 mode 0   # Winter: Heater mode
python3 thermal.py -H 192.168.1.100 mode 1   # Summer: Mining mode
```

---

## Security Notice

The CGMiner API on port 4028 has **no authentication**. Anyone on your network can control your miner.

**Recommendations:**
- Isolate miners on a dedicated VLAN
- Firewall port 4028 from untrusted networks
- Use a strong web UI password

---

## Testing

Run the full suite (unit + property + device integration):

```bash
./scripts/test.sh
```

If devices are offline, you can skip them:

```bash
TK_SKIP_OFFLINE=1 ./scripts/test.sh
```

---

## Website

The project homepage is published with GitHub Pages from the `docs/` folder:
[https://mars-llm.github.io/thermal-key/](https://mars-llm.github.io/thermal-key/)

---

<details>
<summary><h2>Technical Details</h2></summary>

### Authentication Algorithm

Reverse-engineered from firmware:

```python
import hashlib

def compute_auth(password: str, dna: str) -> str:
    """Compute web UI auth token."""
    webpass = hashlib.sha256(password.encode()).hexdigest()
    return hashlib.sha256((webpass[:8] + dna).encode()).hexdigest()[:8]

# DNA = device identifier from CGMiner stats
# Session cookie = auth + webpass[:24]
```

### CGMiner API

Standard CGMiner JSON API on TCP port 4028:

```python
import socket, json

def cgminer_cmd(host, command, param=None):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, 4028))
    req = {"command": command}
    if param:
        req["parameter"] = param
    sock.send(json.dumps(req).encode() + b'\x00')
    return json.loads(sock.recv(65536).decode().rstrip('\x00'))
```

</details>

---

## License

MIT — See [LICENSE](LICENSE)

---

<p align="center">
  <sub>Built for people who prefer terminals over apps</sub><br><br>
  <sub>Avalon Mini 3 and Avalon Nano 3S are trademarks of <a href="https://www.canaan.io">Canaan Inc.</a>
  Product link courtesy of <a href="https://bitcoinbrabant.com/en/product/avalon-mini-3-home-bitcoin-miner/">Bitcoin Brabant</a>.<br>
  This project is not affiliated with or endorsed by Canaan or Bitcoin Brabant.</sub>
</p>
