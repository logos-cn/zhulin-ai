from __future__ import annotations

import ipaddress
import socket
from typing import Iterable
from urllib.parse import urlsplit


class UnsafeOutboundURLError(ValueError):
    pass


_LOCAL_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
}


def _is_unsafe_ip(address: ipaddress._BaseAddress) -> bool:
    return any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _iter_resolved_ips(hostname: str, port: int) -> Iterable[ipaddress._BaseAddress]:
    seen: set[str] = set()
    try:
        results = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError:
        return ()

    resolved: list[ipaddress._BaseAddress] = []
    for family, _socktype, _proto, _canonname, sockaddr in results:
        if family == socket.AF_INET:
            raw_ip = str(sockaddr[0])
        elif family == socket.AF_INET6:
            raw_ip = str(sockaddr[0])
        else:
            continue
        if raw_ip in seen:
            continue
        seen.add(raw_ip)
        try:
            resolved.append(ipaddress.ip_address(raw_ip))
        except ValueError:
            continue
    return resolved


def validate_outbound_base_url(
    base_url: str,
    *,
    allow_private_network: bool,
    resolve_dns: bool,
) -> str:
    value = str(base_url or "").strip()
    if not value:
        raise UnsafeOutboundURLError("接口地址不能为空。")

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeOutboundURLError("接口地址只支持 http 或 https。")
    if not parsed.hostname:
        raise UnsafeOutboundURLError("接口地址缺少有效主机名。")
    if parsed.username or parsed.password:
        raise UnsafeOutboundURLError("接口地址不允许包含用户名或密码。")

    hostname = parsed.hostname.strip().lower()
    if not allow_private_network:
        if hostname in _LOCAL_HOSTNAMES or hostname.endswith(".localhost"):
            raise UnsafeOutboundURLError("普通用户不能将接口地址指向本机或内网。")

        try:
            literal_ip = ipaddress.ip_address(hostname)
        except ValueError:
            literal_ip = None

        if literal_ip is not None and _is_unsafe_ip(literal_ip):
            raise UnsafeOutboundURLError("普通用户不能将接口地址指向本机或内网。")

        if resolve_dns:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            for resolved_ip in _iter_resolved_ips(hostname, port):
                if _is_unsafe_ip(resolved_ip):
                    raise UnsafeOutboundURLError("普通用户不能将接口地址指向本机或内网。")

    return value
