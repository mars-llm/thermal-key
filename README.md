<h1 align="center">Thermal Key</h1>

<p align="center">
  <strong>Local-first CLI control for Avalon Mini 3 & Nano 3S miners</strong><br>
  Monitor, automate, and manage fleets without vendor apps or cloud logins.
</p>

<p align="center">
  <img src="thermal-key.png" alt="Thermal Key" width="640">
</p>

<p align="center">
  <a href="#quick-start"><img src="https://img.shields.io/badge/Quick_Start-blue?style=flat-square" alt="Quick Start"></a>
  <a href="#commands"><img src="https://img.shields.io/badge/Commands-0ea5e9?style=flat-square" alt="Commands"></a>
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
- Locked out? Contact support

</td>
<td width="50%">

### Thermal Key
- 100% local
- Any terminal, any platform
- Fleet management built-in
- Automate everything
- Recover access yourself

</td>
</tr>
</table>

> **Safety note:** miners can overheat or be damaged by wrong settings. Use conservative limits and monitor temps.

---

## Quick Start

> **You'll need:** Python 3.8+ and the miner's IP address

```bash
git clone https://github.com/mars-llm/thermal-key.git
cd thermal-key

python3 thermal.py -H 192.168.1.100 status
python3 thermal.py -H 192.168.1.100 watch
```

---

## Features

- Real-time status: hashrate, temps, fan, power, uptime
- Fleet commands across many devices in parallel
- Safe controls: reboot, fan, mode, pool switch
- Web UI auth tools (auth + getauth)
- Raw CGMiner access when you need it

---

## Commands

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
thermal.py -H IP level 3         # Performance level

# Pool Management
thermal.py -H IP switchpool 0    # Switch active pool
thermal.py -H IP enablepool 1    # Enable pool
thermal.py -H IP disablepool 2   # Disable pool

# Authentication
thermal.py -H IP auth PASSWORD   # Authenticate to web UI
thermal.py -H IP getauth         # Get auth hash (for recovery)

# Advanced
thermal.py -H IP raw COMMAND     # Raw CGMiner API command
thermal.py -H IP ascset "0,..."  # Raw ascset command
thermal.py -H IP ascset-help     # List ascset options
```

---

## Fleet Management

```bash
python3 thermal.py -H 192.168.1.100,192.168.1.101,192.168.1.102 status
```

```
HOST             HASHRATE   TEMP   FAN  POWER   M  UPTIME
----------------------------------------------------------------------
192.168.1.100      40.1 TH/s   63C  100%  1215W   M  2h 15m
192.168.1.101      39.8 TH/s   61C  100%  1210W   M  5h 30m
192.168.1.102      OFFLINE
```

---

## Password Recovery (Optional)

If you are locked out of the web UI, you can retrieve the auth hash with:

```bash
thermal.py -H IP getauth
```

Then use the optional recovery helper:

```bash
python3 password.py -d IP -w wordlist.txt
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

```bash
./scripts/test.sh
```

If devices are offline, you can skip them:

```bash
TK_SKIP_OFFLINE=1 ./scripts/test.sh
```

---

## Website

The project homepage is published from `docs/` via GitHub Pages.

---

## License

MIT — See [LICENSE](LICENSE)
