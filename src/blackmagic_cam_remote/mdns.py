"""Finding the phone on the local network (mDNS / DNS-SD), without adb's own mDNS support.

Debian's adb (the one in the Docker image) is built without mDNS, so `adb mdns services` stays empty even where
multicast works (Linux with host networking). Android advertises Wireless debugging as DNS-SD services:
  _adb-tls-pairing._tcp   while "Pair device with QR code / pairing code" is open (instance = the QR's name)
  _adb-tls-connect._tcp   while Wireless debugging is on (the address to `adb connect`)
This module asks for them directly (one multicast query, standard library only). Docker Desktop on macOS/Windows
doesn't pass multicast into containers, so there it finds nothing and the IP scan in setup.py takes over.
"""
import select
import socket
import struct
import time

GROUP, PORT = "224.0.0.251", 5353
SERVICES = ("_adb-tls-pairing._tcp.local", "_adb-tls-connect._tcp.local")
PTR, SRV, A = 12, 33, 1


def _encode_name(name):
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"


def _query(names, direct):
    header = struct.pack(">HHHHHH", 0, 0, len(names), 0, 0, 0)
    # class IN; with the top bit set ("QU") responders answer the asking socket directly instead of the group
    cls = 0x8001 if direct else 0x0001
    return header + b"".join(_encode_name(n) + struct.pack(">HH", PTR, cls) for n in names)


def _read_name(msg, i):
    labels, jumped, end = [], False, i
    for _ in range(64):  # guard against compression loops
        n = msg[i]
        if n == 0:
            i += 1
            break
        if n & 0xC0 == 0xC0:  # compression pointer
            if not jumped:
                end = i + 2
            i = ((n & 0x3F) << 8) | msg[i + 1]
            jumped = True
            continue
        labels.append(msg[i + 1:i + 1 + n].decode(errors="replace"))
        i += 1 + n
    return ".".join(labels), (end if jumped else i)


def _records(msg):
    """All resource records in a DNS message as (name, type, rdata_offset, rdata_length)."""
    qd, an, ns, ar = struct.unpack(">HHHH", msg[4:12])
    i = 12
    for _ in range(qd):
        _, i = _read_name(msg, i)
        i += 4
    out = []
    for _ in range(an + ns + ar):
        name, i = _read_name(msg, i)
        rtype, _, _, rdlen = struct.unpack(">HHIH", msg[i:i + 10])
        i += 10
        out.append((name.lower(), rtype, i, rdlen))
        i += rdlen
    return out


def _local_ips(near=None):
    """Addresses of the interfaces that lead to the phone (if its IP is known) and to the default route.

    Multicast must go out on the LAN interface explicitly; otherwise the system may pick a virtual one
    (Docker bridges, VPNs) and the phone never sees the query.
    """
    ips = []
    for target in filter(None, (near, "8.8.8.8")):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as p:
                p.connect((target, 9))  # no packet is sent; this only asks the routing table
                ip = p.getsockname()[0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
        except OSError:
            pass
    return ips


def _group_socket(ips):
    """Socket on port 5353 in the mDNS group: receives answers sent to the group (shared with avahi/mDNSResponder)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    try:
        s.bind(("", PORT))
    except OSError:
        s.close()
        return None
    for ip in ips or ["0.0.0.0"]:
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(GROUP) + socket.inet_aton(ip))
        except OSError:
            pass
    return s


def _direct_socket():
    """Socket on its own port: receives direct ("QU") answers. On port 5353 those could go to another program."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.bind(("", 0))
    return s


def _send(socks, ips):
    for s, direct in socks:
        for ip in ips or [None]:
            try:
                if ip:
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
                s.sendto(_query(SERVICES, direct), (GROUP, PORT))
            except OSError:
                pass


def browse(timeout=1.5, near=None):
    """Wireless debugging services on the network: [(instance, service, "ip:port")], like `adb mdns services`.

    Asks twice: a normal query from the group socket (answers go to the group) and a direct ("QU") query from
    its own socket. near: the phone's IP if known (picks the network interface).
    """
    ips = _local_ips(near)
    socks = []
    try:
        g = _group_socket(ips)
        if g:
            socks.append((g, False))
        socks.append((_direct_socket(), True))
    except OSError:
        pass
    if not socks:
        return []
    ptrs, srvs, addrs = {}, {}, {}
    try:
        _send(socks, ips)
        start = time.time()
        resent = False
        while time.time() - start < timeout:
            if not resent and time.time() - start > timeout / 2:
                _send(socks, ips)  # one retry: responders answer the same question at most about once a second
                resent = True
            ready, _, _ = select.select([s for s, _ in socks], [], [], 0.1)
            for s in ready:
                try:
                    msg, (src, _) = s.recvfrom(9000)
                except OSError:
                    continue
                try:
                    for name, rtype, off, rdlen in _records(msg):
                        if rtype == PTR and name in SERVICES:
                            ptrs.setdefault(_read_name(msg, off)[0].lower(), name)
                        elif rtype == SRV:
                            port = struct.unpack(">H", msg[off + 4:off + 6])[0]
                            srvs[name] = (_read_name(msg, off + 6)[0].lower(), port, src)
                        elif rtype == A and rdlen == 4:
                            addrs[name] = socket.inet_ntoa(msg[off:off + 4])
                except (IndexError, struct.error, UnicodeError):
                    continue  # not a message we understand
    finally:
        for s, _ in socks:
            s.close()
    found = []
    for instance, service in ptrs.items():
        if instance in srvs:
            target, port, src = srvs[instance]
            ip = addrs.get(target, src)  # fall back to the sender's address
            found.append((instance[:-len(service) - 1], service.replace(".local", ""), f"{ip}:{port}"))
    return found
