"""uv.lock → GitHub 의존성 그래프 스냅샷(JSON, stdout).

Dependabot을 끄면 uv.lock의 그래프 갱신도 멈춰, 고친 취약점의 알림이 낡은 버전 기준으로
남는다. 이 스크립트가 그 역할을 대신한다 — `.github/workflows/dependency-graph.yml`이
결과를 Dependency submission API로 보낸다.

- direct = 프로젝트가 직접 선언한 의존, 나머지는 indirect
- scope = 런타임 의존에서 닿으면 runtime, dev extra로만 닿으면 development
- 표준 라이브러리만 쓴다(CI에서 설치 없이 돈다)

필요 env: GITHUB_SHA · GITHUB_REF · GITHUB_RUN_ID (Actions가 채운다)
"""

import json
import os
import sys
import tomllib
from datetime import UTC, datetime

LOCK = "uv.lock"


def _purl(pkg: dict) -> str:
    return f"pkg:pypi/{pkg['name']}@{pkg['version']}"


def main() -> None:
    with open(LOCK, "rb") as f:
        packages = tomllib.load(f)["package"]

    root = next(p for p in packages if p.get("source", {}).get("editable") == ".")
    by_name: dict[str, list[dict]] = {}
    for p in packages:
        if p is not root:
            by_name.setdefault(p["name"], []).append(p)

    def resolve(edge: dict) -> dict:
        # 같은 이름이 여러 버전이면 uv가 edge에 version을 적는다.
        cands = by_name[edge["name"]]
        if "version" in edge:
            return next(p for p in cands if p["version"] == edge["version"])
        return cands[0]

    def edges(pkg: dict) -> list[dict]:
        return pkg.get("dependencies", [])

    def expand(edge: dict) -> tuple[dict, list[dict]]:
        # (대상 패키지, 그 패키지로부터 이어지는 edge들) — 요청한 extra의 의존까지 포함.
        target = resolve(edge)
        extra_edges = [
            e
            for extra in edge.get("extra", [])
            for e in target.get("optional-dependencies", {}).get(extra, [])
        ]
        return target, edges(target) + extra_edges

    def reach(start: list[dict]) -> dict[str, set[str]]:
        # purl → 그 패키지가 의존하는 purl들. extra 의존은 요청한 쪽에서만 붙는다.
        graph: dict[str, set[str]] = {}
        stack = list(start)
        while stack:
            target, outs = expand(stack.pop())
            key = _purl(target)
            deps = {_purl(resolve(e)) for e in outs}
            if key in graph and deps <= graph[key]:
                continue
            graph.setdefault(key, set()).update(deps)
            stack.extend(outs)
        return graph

    runtime_direct = edges(root)
    dev_direct = [e for es in root.get("optional-dependencies", {}).values() for e in es]
    runtime = reach(runtime_direct)
    everything = reach(runtime_direct + dev_direct)
    direct = {_purl(resolve(e)) for e in runtime_direct + dev_direct}

    resolved = {
        key: {
            "package_url": key,
            "relationship": "direct" if key in direct else "indirect",
            "scope": "runtime" if key in runtime else "development",
            "dependencies": sorted(deps),
        }
        for key, deps in sorted(everything.items())
    }

    snapshot = {
        "version": 0,
        "sha": os.environ["GITHUB_SHA"],
        "ref": os.environ["GITHUB_REF"],
        "job": {"correlator": "uv-lock-snapshot", "id": os.environ["GITHUB_RUN_ID"]},
        "detector": {
            "name": "puppytalk-uv-lock-snapshot",
            "version": "1.0.0",
            "url": "https://github.com/kyjness/puppytalk-be/blob/main/scripts/uv_lock_snapshot.py",
        },
        "scanned": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifests": {
            LOCK: {"name": LOCK, "file": {"source_location": LOCK}, "resolved": resolved},
        },
    }
    json.dump(snapshot, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
