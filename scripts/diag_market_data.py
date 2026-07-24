#!/usr/bin/env python3
"""Market data layer diagnostic tool.

Diagnoses the Yahoo Finance TLS / connectivity failures seen during
selector runs.  Isolates the fault between:

  1. Python / OpenSSL version
  2. curl_cffi vs requests backend
  3. Surge / system proxy interference
  4. Yahoo Finance rate limiting
  5. DNS resolution path

Usage:
  .venv/bin/python scripts/diag_market_data.py
"""

from __future__ import annotations

import os
import ssl
import socket
import sys
import time
import urllib.request
from datetime import datetime


# ── helpers ──────────────────────────────────────────────────────────────────

def header(title: str) -> None:
    print()
    print("=" * 64)
    print(f"  {title}")
    print("=" * 64)


def ok(label: str, detail: str = "") -> None:
    print(f"  ✅  {label:<40s} {detail}")


def warn(label: str, detail: str = "") -> None:
    print(f"  ⚠️  {label:<40s} {detail}")


def fail(label: str, detail: str = "") -> None:
    print(f"  ❌  {label:<40s} {detail}")


# ── diagnostics ──────────────────────────────────────────────────────────────

def diag_python() -> None:
    header("1. Python & OpenSSL")
    print(f"  Python version : {sys.version.split()[0]}")
    print(f"  Executable     : {sys.executable}")
    print(f"  OpenSSL        : {ssl.OPENSSL_VERSION}")
    paths = ssl.get_default_verify_paths()
    print(f"  CA file        : {paths.cafile}")
    print(f"  CA path        : {paths.capath}")

    # Test SSL context creation
    try:
        ctx = ssl.create_default_context()
        stats = ctx.cert_store_stats()
        print(f"  CA certs loaded: {stats.get('x509_ca', 'N/A')}")
        ok("SSL context", f"{stats.get('x509_ca', 0)} trusted CAs")
    except Exception as exc:
        fail("SSL context", str(exc))


def diag_proxy() -> None:
    header("2. Proxy Environment")
    proxy_vars = [
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
        "ALL_PROXY", "NO_PROXY", "no_proxy",
        "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE",
    ]
    found = 0
    for var in proxy_vars:
        value = os.environ.get(var)
        if value:
            found += 1
            print(f"  {var} = {value}")

    if found == 0:
        ok("Env vars", "no proxy/CA environment variables set")
        print("  (system proxy is handled at the macOS network level)")

    # Check macOS system proxy
    import subprocess
    try:
        output = subprocess.check_output(
            ["scutil", "--proxy"], stderr=subprocess.DEVNULL, text=True
        )
        for line in output.splitlines():
            line = line.strip()
            if any(kw in line for kw in ("HTTPProxy", "HTTPSProxy", "HTTPEnable",
                                         "HTTPSEnable", "HTTPPort", "HTTPSPort",
                                         "ProxyAutoConfig", "SOCKSEnable")):
                print(f"  [system] {line}")
    except Exception:
        print("  [system] unable to read proxy settings")


def diag_dns() -> None:
    header("3. DNS Resolution")
    hosts = [
        "query1.finance.yahoo.com",
        "query2.finance.yahoo.com",
        "fc.yahoo.com",
    ]
    for host in hosts:
        try:
            addrs = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
            ips = sorted({a[4][0] for a in addrs})
            for ip in ips:
                if ip.startswith("198.18."):
                    warn(f"{host}", f"{ip} ← Surge fake IP (proxy active)")
                else:
                    ok(f"{host}", f"{ip}")
        except Exception as exc:
            fail(f"{host}", str(exc))


def diag_https() -> None:
    header("4. HTTPS Connectivity (Python urllib)")

    def test_url(label: str, url: str) -> tuple[bool, str]:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
            })
            resp = urllib.request.urlopen(req, timeout=15)
            return True, f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            return True, f"HTTP {e.code} (rate limited)"
        except Exception as e:
            return False, f"{type(e).__name__}: {e!s}"[:120]

    targets = [
        ("Yahoo Finance v8", "https://query1.finance.yahoo.com/v8/finance/chart/SPY?interval=1d&range=1mo"),
        ("Yahoo Finance v7", "https://query2.finance.yahoo.com/v7/finance/quote?symbols=SPY"),
        ("Yahoo FC",        "https://fc.yahoo.com/"),
    ]
    for label, url in targets:
        success, detail = test_url(label, url)
        if success and "429" in detail:
            warn(label, detail + " — Yahoo is rate-limiting")
        elif success:
            ok(label, detail)
        else:
            fail(label, detail)


def diag_yfinance() -> None:
    header("5. yfinance Backend")

    # yfinance backend selection
    disable_env = os.environ.get("YF_DISABLE_CURL_CFFI", "")
    print(f"  YF_DISABLE_CURL_CFFI = {disable_env!r}" if disable_env
          else "  YF_DISABLE_CURL_CFFI = (not set)")

    has_curl = False
    try:
        from curl_cffi import requests as _curl
        has_curl = True
        print(f"  curl_cffi            : installed (will be used for impersonation)")
    except ImportError:
        print(f"  curl_cffi            : NOT installed")

    if has_curl and not disable_env:
        ok("Active backend", "curl_cffi (TLS impersonation)")
        print("  ⚠️  curl_cffi bundles its own TLS library.")
        print("  ⚠️  This can conflict with Surge proxy MITM certificates.")
        print("  ⚠️  If you see 'TLS connect error: invalid library',")
        print("  ⚠️  set: export YF_DISABLE_CURL_CFFI=1")
    elif has_curl and disable_env:
        warn("Active backend", "requests (curl_cffi disabled by env var)")
    else:
        ok("Active backend", "requests (standard Python SSL)")


def diag_yfinance_fetch() -> None:
    header("6. yfinance Live Fetch Test")

    import yfinance as yf
    tickers = ["SPY", "AVGO", "AAPL", "MSFT", "NVDA"]
    passed = 0
    failed = 0
    empty = 0

    for symbol in tickers:
        start = time.monotonic()
        try:
            tk = yf.Ticker(symbol)
            hist = tk.history(period="1mo")
            elapsed = (time.monotonic() - start) * 1000
            if len(hist) == 0:
                warn(symbol, f"empty response ({elapsed:.0f}ms) — likely rate limited")
                empty += 1
            else:
                ok(symbol, f"{len(hist)} rows, last={hist.index[-1].strftime('%Y-%m-%d')} ({elapsed:.0f}ms)")
                passed += 1
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            msg = str(exc)
            if "TLS connect error" in msg:
                fail(symbol, f"TLS ERROR ({elapsed:.0f}ms) — curl_cffi + Surge conflict")
            elif "429" in msg or "Too Many Requests" in msg:
                warn(symbol, f"rate limited ({elapsed:.0f}ms)")
            else:
                fail(symbol, f"{type(exc).__name__}: {msg[:100]}")
            failed += 1

    print()
    total = passed + failed + empty
    print(f"  Results: {passed}/{total} OK, {empty} empty, {failed} failed")
    if passed == total:
        ok("yfinance", "all tickers fetched successfully")
    elif failed > 0:
        fail("yfinance", f"{failed} failures — check proxy / TLS")
    elif empty > 0:
        warn("yfinance", "some tickers returned empty — Yahoo rate limiting")


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"Market Data Diagnostic  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    diag_python()
    diag_proxy()
    diag_dns()
    diag_https()
    diag_yfinance()
    diag_yfinance_fetch()

    header("Summary")
    print()
    print("  If curl_cffi TLS errors appear:")
    print("    1. Set env var: YF_DISABLE_CURL_CFFI=1")
    print("    2. This forces yfinance to use requests + Python SSL")
    print("    3. Python SSL uses Homebrew OpenSSL certs (Surge-compatible)")
    print()
    print("  If Yahoo 429 rate limiting:")
    print("    1. Reduce batch size (OPENALPHA_MAX_SYMBOLS=15)")
    print("    2. Add delays between batches")
    print("    3. Use EOD data during off-peak hours")
    print()
    print("  If proxy interference:")
    print("    1. Add *.finance.yahoo.com to Surge bypass list")
    print("    2. Or use Surge's DIRECT policy for finance domains")
    print()


if __name__ == "__main__":
    main()
