"""Raspberry Pi -> laptop-local YOLO/grasp-plan HTTP client.

The Pi sends:
  * a current JPEG frame at the calibration resolution
  * current Base-frame Flange pose [x, y, z, rx, ry, rz]

The laptop returns a detection and a Base-frame ``flange_command``.
The Pi still applies its own final workspace/safety validation before motion.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import uuid
from typing import Any
from urllib.parse import urlparse, urlunparse

import cv2
import requests

from . import config


class GraspServerError(RuntimeError):
    """Raised when the laptop service is unreachable or returns an invalid plan."""


def _parsed_server_url():
    parsed = urlparse(config.GRASP_SERVER_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise GraspServerError(
            "Invalid grasp_server_url. Set the laptop LAN address, for example: "
            "http://192.168.0.20:8000/v1/grasp-plan"
        )

    host = parsed.hostname.lower()
    if host in {"laptop_lan_ip", "your_laptop_ip"}:
        raise GraspServerError(
            "grasp_server_url still contains a placeholder. Replace LAPTOP_LAN_IP "
            "with the laptop IPv4 address, for example 192.168.0.20."
        )

    if host in {"localhost", "127.0.0.1", "::1"} and not config.ALLOW_LOOPBACK_SERVER:
        raise GraspServerError(
            "grasp_server_url points to loopback. On the Raspberry Pi, 127.0.0.1 "
            "means the Pi itself, not the laptop. Use the laptop LAN IPv4 address."
        )

    endpoint = parsed.path.rstrip("/")
    if endpoint not in {"/grasp-plan", "/v1/grasp-plan"}:
        raise GraspServerError(
            "Laptop service endpoint must be /v1/grasp-plan (recommended) or "
            f"/grasp-plan, not {parsed.path!r}."
        )
    return parsed


def _health_url() -> str:
    parsed = _parsed_server_url()
    return urlunparse((parsed.scheme, parsed.netloc, "/health", "", "", ""))


def _is_private_or_local_host(host: str) -> bool:
    if host.lower() in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _connection_hint(exc: BaseException) -> str:
    """Give deployment-focused guidance for a direct Pi -> laptop LAN connection."""
    parsed = _parsed_server_url()
    host = parsed.hostname or "<unknown>"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        resolved = sorted(
            {
                item[4][0]
                for item in socket.getaddrinfo(
                    host, port, type=socket.SOCK_STREAM
                )
            }
        )
        resolved_text = ", ".join(resolved)
    except OSError:
        resolved_text = "DNS/IP resolve failed"

    if isinstance(
        exc,
        (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError),
    ):
        if _is_private_or_local_host(host):
            network_advice = (
                "Confirm that the laptop server is running with `python run_server.py`, "
                "that its config uses host = 0.0.0.0 and port = 8000, and that this "
                "Pi and laptop are on the same LAN. Allow inbound TCP 8000 for Python "
                "on the laptop's private-network firewall profile. From the Pi, test "
                f"`curl http://{host}:{port}/health`."
            )
        else:
            network_advice = (
                "The configured host is not a private LAN address. For this direct "
                "laptop deployment, use the laptop's current 192.168.x.x or 10.x.x.x "
                "address, or use a VPN such as Tailscale/WireGuard when the two "
                "machines are on different networks."
            )
        return (
            f"Connection failed to {host}:{port} (resolved: {resolved_text}). "
            f"{network_advice}"
        )

    return (
        f"Connection check failed for {host}:{port} "
        f"(resolved: {resolved_text}): {exc}"
    )


def check_server_health() -> dict[str, Any]:
    """Verify the direct network path and confirm this is the laptop-local service."""
    url = _health_url()
    try:
        response = requests.get(
            url,
            timeout=(config.CONNECT_TIMEOUT_SEC, config.HEALTH_TIMEOUT_SEC),
        )
    except requests.RequestException as exc:
        raise GraspServerError(_connection_hint(exc)) from exc

    if not response.ok:
        raise GraspServerError(
            f"Health check returned HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )
    try:
        payload: dict[str, Any] = response.json()
    except ValueError as exc:
        raise GraspServerError(
            "Health endpoint response is not valid JSON"
        ) from exc

    if payload.get("status") != "ok":
        raise GraspServerError(
            f"Health endpoint reported an error: {payload}"
        )

    expected_runtime = config.EXPECTED_SERVER_RUNTIME
    if expected_runtime and payload.get("runtime") != expected_runtime:
        raise GraspServerError(
            "Connected service is not the expected laptop-local server: "
            f"expected runtime={expected_runtime!r}, "
            f"received runtime={payload.get('runtime')!r}."
        )
    return payload


def request_grasp_plan(frame, flange_coords: list[float]) -> dict[str, Any]:
    """Send a frame and current Flange pose to the laptop without SSH tunneling."""
    if not isinstance(flange_coords, list) or len(flange_coords) != 6:
        raise GraspServerError("flange_coords must contain six values")

    ok, encoded = cv2.imencode(
        ".jpg",
        frame,
        [int(cv2.IMWRITE_JPEG_QUALITY), config.JPEG_QUALITY],
    )
    if not ok:
        raise GraspServerError("JPEG encoding failed")

    request_id = str(uuid.uuid4())
    files = {"image": ("frame.jpg", encoded.tobytes(), "image/jpeg")}
    data = {
        "robot_state": json.dumps(
            {
                "request_id": request_id,
                "flange_coords": [float(value) for value in flange_coords],
            }
        )
    }

    try:
        response = requests.post(
            config.GRASP_SERVER_URL,
            files=files,
            data=data,
            timeout=(config.CONNECT_TIMEOUT_SEC, config.REQUEST_TIMEOUT_SEC),
        )
    except requests.RequestException as exc:
        raise GraspServerError(_connection_hint(exc)) from exc

    if not response.ok:
        raise GraspServerError(
            f"Laptop server HTTP {response.status_code}: {response.text[:800]}"
        )
    try:
        payload: dict[str, Any] = response.json()
    except ValueError as exc:
        raise GraspServerError(
            "Laptop server response is not valid JSON"
        ) from exc

    if payload.get("request_id") not in (None, request_id):
        raise GraspServerError("Laptop server request_id mismatch")
    return payload
