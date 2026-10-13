"""Steam identity challenges for opted-in GUIDs, owned by one manager process.

All connection state is guarded by the service lock, including ACSP mutations.
This does not protect unlinked/unprotected GUIDs or prevent ban evasion with a
new GUID. Owners forwarding codes cannot be detected. Times earned before a
kick remain in acServer result JSON; this module does not change ingestion.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Annotated, Literal
from weakref import WeakKeyDictionary

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app import metrics
from app.auth import guard, require_any
from app.config import settings
from app.db import SessionDep, engine
from app.models import Server, Setting

log = logging.getLogger(__name__)
ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
TTL = 180
Guid = Annotated[str, Field(pattern=r'^[0-9]{17}$')]
Mode = Literal['off', 'warn', 'enforce']
router = APIRouter(tags=['identity'])


@dataclass
class Challenge:
    guid: str
    name: str
    generation: int
    code: str
    expires: float
    state: str = 'pending'
    attempts: int = 3
    timer: asyncio.TimerHandle | None = None


class IdentityService:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.settings_lock = threading.Lock()
        self.clients = WeakKeyDictionary()
        self.protected: set[str] = set()
        self.version = 0
        self.modes: dict[int, str] = {}
        self.generation = 0
        self.attempts: dict[tuple[str, str], deque] = {}
        self.consumed: deque = deque()

    def load(self) -> None:
        with self.lock, Session(engine) as sess:
            row = sess.get(Setting, 'identity.protected')
            self.version = row.value['version'] if row else 0
            self.protected = set(row.value['guids']) if row else set()
            self.modes = {int(r.key.removeprefix('identity.mode.')): r.value['mode']
                          for r in sess.exec(select(Setting).where(Setting.key.startswith('identity.mode.')))}

    def reset(self, client, car_id: int | None = None) -> None:
        with self.lock:
            records = self.clients.get(client, {}) if car_id is None else self.clients.setdefault(client, {})
            for i in list(records) if car_id is None else [car_id]:
                r = records.pop(i, None)
                if r and r.timer:
                    r.timer.cancel()
            if car_id is None:
                self.clients.pop(client, None)

    def start(self, client, car_id: int, resend: bool = False) -> None:
        with self.lock:
            car = client.cars.get(car_id, {})
            guid = car.get('driver_guid')
            records = self.clients.setdefault(client, {})
            if self.modes.get(client.server_id, 'off') == 'off' or guid not in self.protected:
                return
            existing = records.get(car_id)
            if existing and not (resend and existing.state == 'pending' and existing.guid == guid):
                return
            if not settings.site_url:
                log.warning('ACM_SITE_URL is empty; identity challenge skipped for server %s', client.server_id)
                return
            self._prune(time.monotonic())
            used = {r.code for rows in self.clients.values() for r in rows.values()}
            used.update(code for _, _, code in self.consumed)
            value = existing.code if existing else ''.join(secrets.choice(ALPHABET) for _ in range(6))
            while not existing and value in used:
                value = ''.join(secrets.choice(ALPHABET) for _ in range(6))
            message = f'OPR: verifica tu cuenta Steam para jugar. Entra en {settings.site_url.rstrip("/")}/verificar y escribe el código: {value} (vale 3 min).'
            if len(message) > 255:
                log.warning('ACM_SITE_URL is too long for identity chat on server %s', client.server_id)
                return
            if existing:
                from app.live.acsp import encode_send_chat
                client.send(encode_send_chat(car_id, message))
                return
            self.generation += 1
            r = Challenge(guid, car.get('driver_name', ''), self.generation, value, time.monotonic() + TTL)
            records[car_id] = r
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:   # a worker thread (PUT /servers/{id}/identity) or a synchronous double: no running loop here
                running = None
            if running:
                r.timer = running.call_later(TTL, self.expire, client, car_id, r.generation, guid)
            elif getattr(client, 'loop', None):   # the ACSP loop the client lives on: the timer must be armed THERE, or the challenge never expires
                client.loop.call_soon_threadsafe(self._arm, client.loop, client, car_id, r)
            from app.live.acsp import encode_send_chat  # encoders live with the protocol
            client.send(encode_send_chat(car_id, message))

    def _arm(self, loop, client, car_id: int, r: Challenge) -> None:
        """Runs ON the ACSP loop: arm the expiry of a challenge that was started from another thread (unless it was replaced or answered meanwhile)."""
        with self.lock:
            if self.clients.get(client, {}).get(car_id) is r and r.state == 'pending' and r.timer is None:
                r.timer = loop.call_later(max(0.0, r.expires - time.monotonic()), self.expire, client, car_id, r.generation, r.guid)

    def expire(self, client, car_id: int, generation: int, guid: str) -> None:
        with self.lock:
            r = self.clients.get(client, {}).get(car_id)
            if not r or r.state != 'pending' or r.generation != generation or r.guid != guid:
                return
            if client.cars.get(car_id, {}).get('driver_guid') != guid:
                self.reset(client, car_id)
                return
            r.state = 'failed'
            if r.timer:
                r.timer.cancel()
            from app.live.acsp import encode_kick_user, encode_send_chat
            mode = self.modes.get(client.server_id, 'off')
            if mode == 'enforce':
                client.send(encode_kick_user(car_id))
                metrics.enqueue(client.server_id, 'identity_kick', guid=guid, name=r.name)
            elif mode == 'warn':
                client.send(encode_send_chat(car_id, 'OPR: identidad sin verificar. Verifica tu cuenta Steam en la próxima conexión.'))
                metrics.enqueue(client.server_id, 'identity_unverified', guid=guid, name=r.name)

    def hidden(self, client, car_id: int) -> bool:
        with self.lock:
            r = self.clients.get(client, {}).get(car_id)
            return bool(self.modes.get(client.server_id) == 'enforce' and r and r.state != 'verified'
                        and r.guid == client.cars.get(car_id, {}).get('driver_guid'))

    def _prune(self, now: float) -> None:
        for key, hits in list(self.attempts.items()):
            while hits and hits[0] <= now - 60:
                hits.popleft()
            if not hits:
                del self.attempts[key]
        while self.consumed and self.consumed[0][0] <= now:
            self.consumed.popleft()

    def verify(self, guid: str, code: str, origin: str) -> None:
        with self.lock:
            now = time.monotonic()
            self._prune(now)
            keys = [('guid', guid), ('origin', origin)]
            if any(len(self.attempts.get(key, ())) >= limit for key, limit in zip(keys, (5, 30))):
                raise HTTPException(429, 'too many attempts')
            if len(self.attempts) + sum(key not in self.attempts for key in keys) > 10000:
                raise HTTPException(429, 'too many attempts')
            for key in keys:
                self.attempts.setdefault(key, deque()).append(now)
            hits = []
            for client, rows in list(self.clients.items()):
                for car_id, r in list(rows.items()):
                    if r.guid != guid or r.state != 'pending':
                        continue
                    if client.cars.get(car_id, {}).get('driver_guid') != guid:
                        self.reset(client, car_id)
                    elif r.expires <= now:
                        self.expire(client, car_id, r.generation, guid)
                    else:
                        hits.append((client, car_id, r))
            if any(g == guid and hmac.compare_digest(code, used) for _, g, used in self.consumed):
                raise HTTPException(404, 'no pending challenge')
            if not hits:
                raise HTTPException(404, 'no pending challenge')
            match = next(((c, i, r) for c, i, r in hits if hmac.compare_digest(code, r.code)), None)
            if match:
                client, car_id, r = match
                r.state = 'verified'
                if r.timer:
                    r.timer.cancel()
                self.consumed.append((now + TTL, guid, r.code))
                from app.live.acsp import encode_send_chat
                client.send(encode_send_chat(car_id, 'OPR: identidad verificada ✓'))
                return
            client, car_id, r = min(hits, key=lambda hit: hit[2].generation)
            r.attempts -= 1
            if not r.attempts:
                self.expire(client, car_id, r.generation, guid)
                raise HTTPException(429, 'too many attempts; challenge cancelled')
            raise HTTPException(403, f'wrong code; {r.attempts} attempts remaining')

    def status(self, server_id: int) -> dict:
        with self.lock:
            now = time.monotonic()
            pending, verified = [], []
            for client, rows in list(self.clients.items()):
                if client.server_id != server_id:
                    continue
                for car_id, r in list(rows.items()):
                    if r.state == 'pending' and r.expires > now:
                        pending.append({'car_id': car_id, 'guid': r.guid, 'name': r.name, 'expires_in': max(0, r.expires - now)})
                    elif r.state == 'verified':
                        verified.append(car_id)
            return {'mode': self.modes.get(server_id, 'off'), 'pending': pending, 'verified': sorted(verified)}


service = IdentityService()


class VerifyIn(BaseModel):
    guid: Guid
    code: str = Field(pattern=r'^[A-HJ-NP-Za-hj-np-z2-9]{6}$')


class ProtectedIn(BaseModel):
    version: int = Field(strict=True, ge=0)
    guids: list[Guid] = Field(max_length=5000)


class ModeIn(BaseModel):
    mode: Mode


@router.post('/identity/verify', status_code=204, dependencies=[Depends(require_any('reporter', 'admin'))])
async def verify(body: VerifyIn, request: Request) -> Response:
    origin = 'forwarded' if 'x-forwarded-for' in request.headers else request.client.host if request.client else 'unknown'
    service.verify(body.guid, body.code.upper(), origin)
    return Response(status_code=204)


@router.put('/identity/protected', dependencies=[Depends(require_any('reporter', 'admin'))])
def protected_put(body: ProtectedIn, sess: SessionDep) -> dict:
    with service.settings_lock:
        row = sess.get(Setting, 'identity.protected')
        if body.version < (row.value['version'] if row else 0):
            return {'applied': False}
        sess.merge(Setting(key='identity.protected', value=body.model_dump()))
        sess.commit()
        with service.lock:
            service.version, service.protected = body.version, set(body.guids)
            for client, rows in list(service.clients.items()):
                for car_id, r in list(rows.items()):
                    if r.guid not in service.protected:
                        service.reset(client, car_id)
        return {'applied': True}


@router.get('/identity/protected', dependencies=[Depends(guard('steward'))])
def protected_get() -> dict:
    with service.lock:
        return {'version': service.version, 'count': len(service.protected)}


def _server(sess: Session, server_id: int) -> None:
    if not sess.get(Server, server_id):
        raise HTTPException(404, 'server not found')


@router.get('/servers/{server_id}/identity', dependencies=[Depends(guard('steward'))])
def server_get(server_id: int, sess: SessionDep) -> dict:
    _server(sess, server_id)
    return service.status(server_id)


@router.put('/servers/{server_id}/identity', dependencies=[Depends(guard('steward'))])
def server_put(server_id: int, body: ModeIn, sess: SessionDep) -> dict:
    _server(sess, server_id)
    with service.settings_lock:
        sess.merge(Setting(key=f'identity.mode.{server_id}', value=body.model_dump()))
        sess.commit()
        with service.lock:
            previous = service.modes.get(server_id, 'off')
            service.modes[server_id] = body.mode
            if previous != body.mode:
                for client in list(service.clients):
                    if client.server_id == server_id:
                        service.reset(client)
                        for car_id in list(client.cars):
                            service.start(client, car_id)
            return service.status(server_id)
