"""Loopback-only controls; credentials are never returned by status endpoints."""
import ipaddress
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Literal

from . import remote_client, remote_host


def loopback_control(request: Request):
    peer = request.client.host if request.client else ""
    try:
        local = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        local = False
    origin = request.headers.get("origin")
    if not local or (origin and not re.fullmatch(
            r"http://(127\.0\.0\.1|localhost|\[::1\]):30(0[0-9]|1[0-9]|2[0-9]|30)", origin)):
        raise HTTPException(403, "Local control only")


router = APIRouter(prefix="/api/remote", dependencies=[Depends(loopback_control)])


class ClientSettings(BaseModel):
    mode: Literal["local", "remote"]
    code: str | None = Field(default=None, max_length=4096)


class HostStart(BaseModel):
    address: str = Field(max_length=64)
    port: int = Field(default=8766, ge=1024, le=65535)


class HostLease(BaseModel):
    lease: str = Field(max_length=100)


@router.get("/client")
def client_state():
    config = remote_client.read_config()
    return {"mode": config["mode"], "configured": bool(config.get("token")),
            "url": config.get("url", ""), "host": remote_host.controller.status()}


@router.post("/client/check")
def check_client(payload: ClientSettings):
    try:
        config = remote_client.decode_code(payload.code) if payload.code else remote_client.read_config()
        if not config.get("token"):
            raise ValueError("Введите код подключения")
        result = remote_client.probe(config)
        # A machine may not send tasks to itself while holding its own GPU slot.
        if remote_host.HOST_CONFIG.exists() and result["host_id"] == remote_host.credentials()["id"]:
            raise ValueError("Это код текущего компьютера. Введите его на ноутбуке.")
        return result
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/client")
def configure_client(payload: ClientSettings):
    config = remote_client.read_config()
    try:
        if payload.mode == "remote":
            if remote_host.controller.status()["running"]:
                raise ValueError("Сначала закройте адаптер на этом компьютере")
            check_client(payload)
            if payload.code:
                config = remote_client.decode_code(payload.code)
        config["mode"] = payload.mode
        remote_client.save_config(config)
        return client_state()
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/host/start")
def start_host(payload: HostStart):
    try:
        return remote_host.controller.start(payload.address, payload.port)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/host/heartbeat")
def host_heartbeat(payload: HostLease):
    try:
        remote_host.controller.heartbeat(payload.lease)
        return {"ok": True, **remote_host.controller.status()}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/host/stop")
def stop_host(payload: HostLease | None = None):
    try:
        remote_host.controller.stop(payload.lease if payload else None)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
