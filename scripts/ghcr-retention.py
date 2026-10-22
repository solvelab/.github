#!/usr/bin/env python3
"""Mantém as KEEP versões com tag mais recentes de uma imagem do GHCR e apaga o resto.

Portado do fluxo da DriveZoneFivem (`DriveZoneFivem/.github`, backend-drivezone#78), com a mesma
garantia: os manifestos filhos de cada versão mantida (plataformas e atestações do buildx, em
qualquer nível de índice) são lidos no próprio registro, e nada é apagado antes de o conjunto a
manter estar completo. Não usa a `actions/delete-package-versions` porque ela conta cada manifesto
filho como versão e pode apagar a plataforma de uma imagem mantida, deixando o `docker pull` quebrado.

Ambiente:
- GH_TOKEN: com `packages: write` (CI) ou `delete:packages` (máquina de quem mantém).
- ORG, PACKAGE: dono e nome da imagem.
- KEEP (padrão 5): quantas versões com tag manter.
- TAG_PATTERN (padrão: todas): regex das tags que entram na contagem. Versão cujas tags não casam é
  de outra família (ex.: `octopus:probe-*`, fixada por digest no código) e fica inteira.
- GRACE_MIN (padrão 60): versão sem tag criada há menos que isso nunca é apagada, porque pode ser
  filho de um push de outra execução que ainda não gravou a tag.
- DRY_RUN: `true` só lista.

A versão com a tag `latest` nunca é apagada, mesmo que a tag tenha sido movida para uma versão antiga.

Poda avulsa: `ORG=solvelab PACKAGE=feldt DRY_RUN=true GH_TOKEN=$(gh auth token)
python3 scripts/ghcr-retention.py`. Só usa a biblioteca padrão.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


class Unlisted(Exception):
    """Manifesto que existe no registro mas a API não lista: nada pode ser apagado."""


def request(url: str, headers: dict[str, str], method: str = "GET"):
    req = urllib.request.Request(url, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        return (json.loads(body) if body else None), resp.headers.get("Link", "")


def list_versions(org: str, pkg: str, headers: dict[str, str]) -> list[dict]:
    url = f"{API}/orgs/{org}/packages/container/{pkg}/versions?per_page=100"
    versions: list[dict] = []
    while url:
        page, link = request(url, headers)
        versions.extend(page)
        nxt = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = nxt.group(1) if nxt else ""
    return versions


def is_attestation(child: dict) -> bool:
    ref_type = child.get("annotations", {}).get("vnd.docker.reference.type")
    return ref_type == "attestation-manifest" or child.get("platform", {}).get("os") == "unknown"


def main() -> int:
    token = os.environ["GH_TOKEN"]
    org, pkg = os.environ["ORG"], os.environ["PACKAGE"]
    keep_n = int(os.environ.get("KEEP") or 5)
    grace_min = int(os.environ.get("GRACE_MIN") or 60)
    dry = os.environ.get("DRY_RUN", "false") == "true"
    pattern = re.compile(os.environ.get("TAG_PATTERN") or "")
    if keep_n < 1 or grace_min < 1:
        print(f"::error::KEEP={keep_n}, GRACE_MIN={grace_min}: os dois precisam ser pelo menos 1")
        return 1
    api = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28"}

    try:
        versions = list_versions(org, pkg, api)
    except urllib.error.HTTPError as e:
        print(f"::error::não consegui listar as versões de {pkg}: HTTP {e.code}")
        return 1
    by_digest = {v["name"]: v for v in versions}

    def tags(v: dict) -> list[str]:
        return v["metadata"]["container"]["tags"]

    def ours(v: dict) -> bool:
        return any(pattern.search(t) for t in tags(v))

    tagged = sorted((v for v in versions if tags(v)), key=lambda v: v["created_at"], reverse=True)
    if not any(ours(v) for v in tagged):
        print(f"{pkg}: nenhuma versão com tag casando com {pattern.pattern!r} — nada a fazer")
        return 0

    basic = base64.b64encode(f"x:{token}".encode()).decode()
    reg, _ = request(f"https://ghcr.io/token?scope=repository:{org.lower()}/{pkg}:pull",
                     {"Authorization": f"Basic {basic}"})
    reg_headers = {"Authorization": f"Bearer {reg['token']}", "Accept": MANIFEST_TYPES}
    base = f"https://ghcr.io/v2/{org.lower()}/{pkg}/manifests"

    def descendants(digest: str) -> tuple[set[int], bool]:
        """Ids de todos os manifestos abaixo de `digest`, em qualquer nível, e se a imagem ainda faz
        `docker pull`: só a falta de uma plataforma quebra; atestação ausente não."""
        manifest, _ = request(f"{base}/{digest}", reg_headers)
        ids, intact = set(), True
        for child in manifest.get("manifests", []):
            if child["digest"] in by_digest:
                ids.add(by_digest[child["digest"]]["id"])
                sub, sub_intact = descendants(child["digest"])
                ids |= sub
                intact = intact and sub_intact
                continue
            try:
                request(f"{base}/{child['digest']}", reg_headers, "HEAD")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                if not is_attestation(child):
                    intact = False  # plataforma já apagada: a imagem não faz mais `docker pull`
                continue
            raise Unlisted(child["digest"])
        return ids, intact

    keep: set[int] = set()
    kept_tagged, broken = [], []
    try:
        for v in tagged:
            pinned = not ours(v) or "latest" in tags(v)
            if not pinned and len(kept_tagged) >= keep_n:
                continue
            ids, intact = descendants(v["name"])
            if pinned:
                # Outra família de tag ou `latest`: fica inteira, sem contar entre as KEEP.
                keep.add(v["id"])
                keep |= ids
                if ours(v) and intact:
                    kept_tagged.append(v)
                continue
            if not intact:
                broken.append(v)
                continue
            kept_tagged.append(v)
            keep.add(v["id"])
            keep |= ids
    except Unlisted as e:
        print(f"::error::o manifesto {e} de uma versão mantida não aparece na lista — abortando sem "
              "apagar")
        return 1
    if not kept_tagged:
        print(f"::error::{pkg}: nenhuma versão com tag inteira — abortando sem apagar")
        return 1
    for v in broken:
        print(f"::warning::{','.join(tags(v))} já estava quebrada (plataforma apagada antes) — "
              "entra na poda")

    grace = datetime.timedelta(minutes=grace_min)
    now = datetime.datetime.now(datetime.timezone.utc)

    def recent(v: dict) -> bool:
        created = datetime.datetime.fromisoformat(v["created_at"].replace("Z", "+00:00"))
        return not tags(v) and now - created < grace

    delete = [v for v in versions if v["id"] not in keep and not recent(v)]
    print(f"mantidas: {' | '.join(','.join(tags(v)) for v in kept_tagged)}")
    mode = " (simulação)" if dry else ""
    print(f"{pkg}: {len(versions)} versões · mantém {len(versions) - len(delete)} "
          f"({len(kept_tagged)} com tag) · apaga {len(delete)}{mode}")
    if dry or not delete:
        return 0

    failed = 0
    for v in delete:
        try:
            request(f"{API}/orgs/{org}/packages/container/{pkg}/versions/{v['id']}", api, "DELETE")
        except (urllib.error.URLError, TimeoutError) as e:
            failed += 1
            print(f"::warning::falha ao apagar a versão {v['id']}: {getattr(e, 'code', e)}")
    print(f"apagadas {len(delete) - failed}, falhas {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
