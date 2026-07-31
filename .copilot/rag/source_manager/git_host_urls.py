from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from .errors import SourceManagerError

GIT_SOURCE_TYPES = frozenset({"github", "gitlab", "azure_devops", "git"})
HOSTED_GIT_SOURCE_TYPES = frozenset({"github", "gitlab", "azure_devops"})
SOURCE_LABELS = {
    "github": "GitHub",
    "gitlab": "GitLab",
    "azure_devops": "Azure DevOps",
    "git": "その他のGit",
}
SOURCE_MENU_LABELS = {
    "github": "GitHub",
    "gitlab": "GitLab",
    "azure_devops": "Azure DevOps",
    "git": "その他のGitサーバー（Webリンク自動生成なし）",
}
LINK_STRATEGIES = {
    "github": "github-blob",
    "gitlab": "gitlab-blob",
    "azure_devops": "azure-devops-item",
}


def make_repository_link(
    source_type: str,
    repository_web_url: Any,
    *,
    ref: str,
) -> dict[str, Any] | None:
    kind = str(source_type or "").strip().lower()
    if kind == "git":
        return None
    if kind not in HOSTED_GIT_SOURCE_TYPES:
        raise SourceManagerError("unsupported Git Source type")
    branch = str(ref or "").strip()
    if not branch or len(branch) > 300 or any(ord(char) < 32 for char in branch):
        raise SourceManagerError("Git branch/ref is invalid")
    return {
        "enabled": True,
        "strategy": LINK_STRATEGIES[kind],
        "settings": {
            "repository_url": normalize_repository_web_url(
                kind, repository_web_url
            ),
            "ref": branch,
            "permalink_enabled": False,
        },
    }


def normalize_repository_web_url(source_type: str, value: Any) -> str:
    kind = str(source_type or "").strip().lower()
    if kind not in HOSTED_GIT_SOURCE_TYPES:
        raise SourceManagerError("Git provider does not support automatic links")
    text = str(value or "").strip()
    split = urlsplit(text)
    if (
        not text
        or len(text) > 4096
        or split.scheme.casefold() not in {"http", "https"}
        or not split.hostname
        or split.username is not None
        or split.password is not None
        or split.query
        or split.fragment
        or any(char.isspace() or ord(char) < 32 for char in text)
    ):
        raise SourceManagerError(
            "リポジトリのWeb URLは認証情報なしのHTTP(S) URLで入力してください。"
        )
    decoded_path = unquote(split.path)
    components = [part for part in decoded_path.strip("/").split("/") if part]
    if any(part in {".", ".."} or "\\" in part for part in components):
        raise SourceManagerError("リポジトリのWeb URLに不正なパスがあります。")
    path = split.path.rstrip("/")
    if path.casefold().endswith(".git"):
        path = path[:-4]
        if components:
            components[-1] = components[-1][:-4]
    lowered = decoded_path.casefold()
    if kind == "github":
        if len(components) < 2 or any(
            marker in lowered for marker in ("/blob/", "/tree/")
        ):
            raise SourceManagerError(
                "GitHubのWeb URLはリポジトリのトップURLを入力してください。"
            )
    elif kind == "gitlab":
        if len(components) < 2 or "/-/" in lowered:
            raise SourceManagerError(
                "GitLabのWeb URLはプロジェクトのトップURLを入力してください。"
            )
    else:
        _validate_azure_web_root(split.hostname or "", components)
    return urlunsplit(
        (
            split.scheme.casefold(),
            _host_netloc(split.hostname or "", split.port),
            path,
            "",
            "",
        )
    ).rstrip("/")


def _validate_azure_web_root(hostname: str, components: list[str]) -> None:
    host = hostname.casefold()
    parts = [part.casefold() for part in components]
    valid = (
        host == "dev.azure.com"
        and len(parts) == 4
        and parts[2] == "_git"
    ) or (
        host.endswith(".visualstudio.com")
        and host != ".visualstudio.com"
        and (
            (len(parts) == 3 and parts[1] == "_git")
            or (
                len(parts) == 4
                and parts[0] == "defaultcollection"
                and parts[2] == "_git"
            )
        )
    )
    if not valid:
        raise SourceManagerError(
            "Azure DevOpsのWeb URLは "
            "https://dev.azure.com/{organization}/{project}/_git/{repository} "
            "形式で入力してください。"
        )


def normalize_clone_url(source_type: str, value: Any) -> str:
    kind = str(source_type or "").strip().lower()
    text = str(value or "").strip()
    if kind == "azure_devops":
        split = urlsplit(text)
        host = str(split.hostname or "").casefold()
        if (
            split.scheme.casefold() in {"http", "https"}
            and split.username is not None
            and split.password is None
            and (host == "dev.azure.com" or host.endswith(".visualstudio.com"))
        ):
            text = urlunsplit(
                (
                    split.scheme,
                    _host_netloc(split.hostname or "", split.port),
                    split.path,
                    split.query,
                    split.fragment,
                )
            )
    from . import providers

    return providers._validate_git_fetch_url(text)


def propose_repository_web_url(source_type: str, clone_url: Any) -> str:
    try:
        return derive_repository_web_url(source_type, clone_url)
    except SourceManagerError:
        return ""


def derive_repository_web_url(source_type: str, clone_url: Any) -> str:
    kind = str(source_type or "").strip().lower()
    if kind not in HOSTED_GIT_SOURCE_TYPES:
        raise SourceManagerError("Git provider does not support automatic links")
    text = str(clone_url or "").strip()
    split = urlsplit(text)
    host = ""
    path = ""
    port: int | None = None
    if split.scheme.casefold() in {"http", "https", "ssh"}:
        host = str(split.hostname or "")
        port = split.port if split.scheme.casefold() != "ssh" else None
        path = split.path.lstrip("/")
    else:
        match = re.fullmatch(r"[^@\s]+@([^:\s]+):(.+)", text)
        if not match:
            raise SourceManagerError("clone URLからWeb URLを推定できません。")
        host, path = match.groups()
    path = path.rstrip("/")
    if path.casefold().endswith(".git"):
        path = path[:-4]
    if kind == "azure_devops" and host.casefold() == "ssh.dev.azure.com":
        parts = [part for part in path.split("/") if part]
        if len(parts) != 4 or parts[0].casefold() != "v3":
            raise SourceManagerError("Azure DevOps SSH clone URLが不正です。")
        candidate = "https://dev.azure.com/" + "/".join(
            (parts[1], parts[2], "_git", parts[3])
        )
    else:
        if not host or not path:
            raise SourceManagerError("clone URLからWeb URLを推定できません。")
        candidate = urlunsplit(
            ("https", _host_netloc(host, port), "/" + path, "", "")
        )
    return normalize_repository_web_url(kind, candidate)


def _host_netloc(hostname: str, port: int | None) -> str:
    host = str(hostname or "")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return host if port is None else f"{host}:{int(port)}"


__all__ = [
    "GIT_SOURCE_TYPES",
    "HOSTED_GIT_SOURCE_TYPES",
    "SOURCE_LABELS",
    "SOURCE_MENU_LABELS",
    "derive_repository_web_url",
    "make_repository_link",
    "normalize_clone_url",
    "normalize_repository_web_url",
    "propose_repository_web_url",
]
