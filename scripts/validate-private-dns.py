#!/usr/bin/env python3
import argparse
import ipaddress
import socket
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that Azure data-plane names resolve only to private addresses"
    )
    parser.add_argument("host", nargs="+", help="Host names to resolve from inside the backup VNet")
    args = parser.parse_args()
    failed = False
    for host in args.host:
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = sorted({item[4][0] for item in records})
        invalid = [address for address in addresses if not ipaddress.ip_address(address).is_private]
        print(f"{host}: {', '.join(addresses) or 'unresolved'}")
        if not addresses or invalid:
            failed = True
            print(f"ERROR: public or missing resolution for {host}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
