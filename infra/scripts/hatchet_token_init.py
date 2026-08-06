#!/usr/bin/env python3
"""One-shot bootstrap do HATCHET_CLIENT_TOKEN dos workers (044/DAI-708).

Fluxo (provado ao vivo no spike T001 — ver roadmap 044): espera a API do
Hatchet responder -> login com HATCHET_ADMIN_EMAIL/PASSWORD -> resolve o
tenant (1o da conta) -> cria um API token novo -> grava em TOKEN_PATH (0600).

RN-02 (design v3, pós-review round 2): token existente com >30d de validade
(claim `exp` do JWT, decodificado localmente, sem chamar a API) -> no-op.
O Bearer de api-token não autentica a REST do hatchet-lite, então não dá pra
validar contra o servidor sem gastar o próprio bootstrap — daí a checagem
local do exp. <30d, ausente, ilegível ou HATCHET_TOKEN_FORCE=1 -> gera novo.
NUNCA revoga o antigo: revogar sob worker rodando derruba produção; a
expiração natural (90d) já limita o conjunto vivo a ~2 tokens. Isso fecha o
achado do devil (round 1: token novo a cada `up -d` acumulava sem limite).

O cookie do login é domain-bound ao SERVER_URL (ex.: localhost) — chamando
http://hatchet:8888 o cookiejar padrão o descartaria. Por isso o Set-Cookie
é capturado e reenviado manualmente como header Cookie, sem urllib.cookiejar.

RN-01: o token da env dos workers tem precedência sobre o arquivo. Quando
HATCHET_CLIENT_TOKEN já vem preenchido, o bootstrap é dispensável e sai 0 sem
tentar login — senão uma credencial de admin inválida derruba um deploy que
nem precisa dela (DAI: api-gateway em Created no sandbox-os, 03-04/08/2026).

Envs:
  HATCHET_API_URL          default http://hatchet:8888
  HATCHET_CLIENT_TOKEN     token pronto (RN-01) -> no-op, nem tenta login
  HATCHET_ADMIN_EMAIL      obrigatório (fail-closed — RN-03)
  HATCHET_ADMIN_PASSWORD   obrigatório (fail-closed — RN-03)
  TOKEN_PATH               default /run/hatchet/token
  HATCHET_TOKEN_FORCE      "1" força regeneração mesmo com token válido

RN-04: zero segredo em log — nunca imprime o token nem a senha, só status HTTP.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

API_URL = os.environ.get("HATCHET_API_URL", "http://hatchet:8888")
TOKEN_PATH = os.environ.get("TOKEN_PATH", "/run/hatchet/token")
WAIT_TIMEOUT_S = 120
WAIT_INTERVAL_S = 3
MIN_REMAINING_S = 30 * 24 * 3600  # RN-02: abaixo disso, regenera


def _fail(step: str, detail: str) -> None:
    print(f"hatchet_token_init: falhou em '{step}': {detail}", file=sys.stderr)
    sys.exit(1)


def _request(method: str, path: str, body: dict | None = None, cookie: str | None = None):
    """Chama a API do hatchet. Retorna (status, headers, json_body)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API_URL}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, resp.headers, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {}
        return e.code, e.headers, parsed


def _join_cookies(set_cookie_headers: list[str]) -> str:
    """Junta os pares nome=valor de cada Set-Cookie num único header Cookie."""
    pairs = [h.split(";", 1)[0].strip() for h in set_cookie_headers if h]
    return "; ".join(pairs)


def _jwt_exp(token: str) -> int:
    """Decodifica o claim `exp` (epoch) do payload do JWT, sem validar assinatura
    (só precisamos do exp pra decidir reuso local — a API é quem valida de fato)."""
    payload_b64 = token.split(".")[1]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    return int(json.loads(base64.urlsafe_b64decode(padded))["exp"])


def _should_regenerate(existing_token: str | None, force: bool, now: float) -> bool:
    """RN-02: regenera se forçado, sem token, ou <30d de validade. Decodificação
    defensiva — payload ilegível conta como "sem token", regenera."""
    if force or existing_token is None:
        return True
    try:
        exp = _jwt_exp(existing_token)
    except Exception:
        return True
    return (exp - now) <= MIN_REMAINING_S


def _read_existing_token() -> str | None:
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            token = f.read().strip()
        return token or None
    except OSError:
        return None


def _first_tenant(memberships: dict) -> tuple[str, str]:
    rows = memberships.get("rows") or []
    if not rows:
        raise ValueError("nenhuma membership retornada")
    tenant = rows[0]["tenant"]
    tenant_id = tenant["metadata"]["id"]
    slug = tenant.get("name") or tenant.get("slug") or tenant_id
    return tenant_id, slug


def wait_for_api() -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{API_URL}/", timeout=5)
            return
        except urllib.error.HTTPError:
            return  # qualquer resposta HTTP conta como "no ar"
        except (urllib.error.URLError, OSError):
            time.sleep(WAIT_INTERVAL_S)
    _fail("espera ativa", f"API não respondeu em {WAIT_TIMEOUT_S}s ({API_URL})")


def login(email: str, password: str) -> str:
    status, headers, _ = _request("POST", "/api/v1/users/login", {"email": email, "password": password})
    if status != 200:
        _fail("login", f"HTTP {status}")
    cookie = _join_cookies(headers.get_all("Set-Cookie") or [])
    if not cookie:
        _fail("login", "resposta sem Set-Cookie")
    return cookie


def resolve_tenant(cookie: str) -> tuple[str, str]:
    status, _, body = _request("GET", "/api/v1/users/memberships", cookie=cookie)
    if status != 200:
        _fail("resolver tenant", f"HTTP {status}")
    try:
        return _first_tenant(body)
    except (KeyError, ValueError) as e:
        _fail("resolver tenant", str(e))


def create_token(cookie: str, tenant_id: str) -> str:
    name = f"worker-{int(time.time())}"
    status, _, body = _request(
        "POST", f"/api/v1/tenants/{tenant_id}/api-tokens", {"name": name}, cookie=cookie
    )
    if status != 200:
        _fail("criar api-token", f"HTTP {status}")
    token = body.get("token")
    if not token:
        _fail("criar api-token", "resposta sem campo 'token'")
    return token


def write_token(token: str) -> None:
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)


def _env_token_wins(env_token: str | None, force: bool, now: float) -> bool:
    """RN-01: token da env dispensa o bootstrap — mas só enquanto ele ainda vale.

    Reusa o mesmo critério do arquivo (RN-02): FORCE, ausente, ilegível ou <30d
    de validade não dispensa nada. Sem isso um token expirado (ou lixo colado no
    .env) viraria no-op silencioso e mataria o auto-heal — os serviços leem a env
    antes do arquivo, então um token morto ali deixa o dispatch mudo sem alarme.
    """
    return not _should_regenerate(env_token, force, now)


def main() -> int:
    force = os.environ.get("HATCHET_TOKEN_FORCE") == "1"
    env_token = os.environ.get("HATCHET_CLIENT_TOKEN")
    if _env_token_wins(env_token, force, time.time()):
        print(
            "hatchet_token_init: HATCHET_CLIENT_TOKEN veio da env e ainda é válido "
            "(RN-01), bootstrap dispensável (HATCHET_TOKEN_FORCE=1 para gerar mesmo assim)"
        )
        return 0
    if env_token:
        print(
            "hatchet_token_init: ATENÇÃO — HATCHET_CLIENT_TOKEN da env está "
            "expirado/ilegível/perto de vencer. Os serviços leem a env ANTES do "
            "arquivo: gerar o token abaixo NÃO os cura, atualize a env.",
            file=sys.stderr,
        )

    email = os.environ.get("HATCHET_ADMIN_EMAIL")
    password = os.environ.get("HATCHET_ADMIN_PASSWORD")
    if not email or not password:
        _fail("envs", "HATCHET_ADMIN_EMAIL/HATCHET_ADMIN_PASSWORD obrigatórios")

    existing = _read_existing_token()
    if not _should_regenerate(existing, force, time.time()):
        exp_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_jwt_exp(existing)))
        print(
            f"hatchet_token_init: token existente válido até {exp_iso}, mantendo "
            "(HATCHET_TOKEN_FORCE=1 para forçar)"
        )
        return 0

    print(f"hatchet_token_init: aguardando {API_URL} ...")
    wait_for_api()
    print("hatchet_token_init: API no ar, autenticando...")
    cookie = login(email, password)
    tenant_id, slug = resolve_tenant(cookie)
    token = create_token(cookie, tenant_id)
    write_token(token)
    print(f"hatchet_token_init: token gravado em {TOKEN_PATH} (tenant {slug})")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        # ponytail: checagem da lógica pura (parsing), sem rede — o fluxo HTTP
        # em si só se prova contra o hatchet-lite real (T007/CA-01).
        assert _join_cookies(["a=1; Path=/", "b=2; HttpOnly"]) == "a=1; b=2"
        assert _join_cookies([]) == ""
        assert _first_tenant(
            {"rows": [{"tenant": {"metadata": {"id": "t1"}, "name": "acme"}}]}
        ) == ("t1", "acme")
        try:
            _first_tenant({"rows": []})
            raise AssertionError("deveria levantar ValueError p/ rows vazio")
        except ValueError:
            pass

        def _fake_jwt(payload: dict) -> str:
            body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
            return f"header.{body}.sig"

        now = time.time()
        futuro = _fake_jwt({"exp": now + 40 * 86400})
        perto = _fake_jwt({"exp": now + 5 * 86400})
        assert _should_regenerate(futuro, False, now) is False, "exp futuro deveria manter"
        assert _should_regenerate(perto, False, now) is True, "exp <30d deveria regenerar"
        assert _should_regenerate("lixo-nao-jwt", False, now) is True, "payload ilegível deveria regenerar"
        assert _should_regenerate(_fake_jwt({"sem_exp": 1}), False, now) is True, "sem claim exp deveria regenerar"
        assert _should_regenerate(futuro, True, now) is True, "FORCE deveria regenerar mesmo com exp futuro"
        assert _should_regenerate(None, False, now) is True, "sem token deveria regenerar"

        assert _env_token_wins(futuro, False, now) is True, "env com token válido dispensa"
        assert _env_token_wins(perto, False, now) is False, "env com <30d NÃO dispensa"
        assert _env_token_wins(futuro, True, now) is False, "FORCE ignora o token da env"
        assert _env_token_wins(None, False, now) is False, "sem token na env, bootstrap roda"
        assert _env_token_wins("", False, now) is False, "env vazia conta como ausente"
        assert _env_token_wins("lixo-colado-no-env", False, now) is False, "lixo na env não dispensa"
        assert _env_token_wins(_fake_jwt({"exp": now - 86400}), False, now) is False, "expirado não dispensa"

        print("selftest OK")
        sys.exit(0)
    sys.exit(main())
