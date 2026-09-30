"""Deterministic, bounded discovery using the configured read-only MCP tools."""
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

SONAR_PROJECTS = 'sonar_search_my_sonarqube_projects'
ADO_PROJECTS = 'ado_projects_list'
ADO_REPOSITORIES = 'ado_repositories_list'
READ_TOOLS = (SONAR_PROJECTS, ADO_PROJECTS, ADO_REPOSITORIES)


@dataclass(frozen=True)
class DiscoveryResult:
    repositories: dict
    findings: list
    sonar_projects: int
    repositories_seen: int


def repository_url(value):
    if not isinstance(value, str):
        raise ValueError('repository URL is missing')
    p = urlsplit(value)
    if p.scheme != 'https' or not p.hostname or p.password or p.query or p.fragment:
        raise ValueError('repository URL must be credential-free HTTPS')
    # Azure DevOps returns an organization username in remoteUrl. Never retain it.
    host = p.hostname + (f':{p.port}' if p.port else '')
    return urlunsplit((p.scheme, host, p.path.rstrip('/'), '', ''))


def url_key(value):
    return unquote(repository_url(value)).casefold()


def match_projects(projects, repositories, explicit):
    enabled = {}
    for r in repositories:
        if r.get('isDisabled') or r.get('isInMaintenance'):
            continue
        try:
            url = repository_url(r.get('remoteUrl'))
        except ValueError:
            continue
        branch = r.get('defaultBranch', '')
        if not branch.startswith('refs/heads/') or not branch[11:]:
            continue
        enabled[url_key(url)] = {**r, 'remoteUrl': url}
    matched, findings = {}, []
    for p in projects:
        key = p['key']
        override = explicit.get(key)
        if override:
            candidate = enabled.get(url_key(override['repository_url']))
            candidates = [candidate] if candidate else []
        else:
            # Exact case-insensitive name matches only. No fuzzy/suffix guessing.
            names = {p.get('name', '').casefold(), key.partition('_')[2].casefold()}
            names.discard('')
            candidates = [r for r in enabled.values() if r['name'].casefold() in names]
        if len(candidates) != 1:
            findings.append({'project_hint': key, 'reason_code': 'ambiguous_repository' if len(candidates) > 1 else 'unresolved_repository'})
            continue
        r = candidates[0]
        matched[key] = {'repository_url': r['remoteUrl'], 'integration_ref': override['integration_ref'] if override else r['defaultBranch'][11:]}
    return DiscoveryResult(matched, findings, len(projects), len(repositories))


async def discover(read, explicit, *, max_pages=20, page_size=100, max_projects=200):
    sonar, seen = [], set()
    for page in range(1, max_pages + 1):
        data = await read(SONAR_PROJECTS, {'pageIndex': page, 'pageSize': page_size})
        rows = data.get('components')
        total = data.get('paging', {}).get('total')
        if not isinstance(rows, list) or type(total) is not int or total < 0:
            raise ValueError('Sonar inventory is incomplete')
        for row in rows:
            key = row.get('key')
            if not isinstance(key, str) or not key or key in seen:
                raise ValueError('Sonar inventory contains repeated or invalid projects')
            seen.add(key)
            sonar.append(row)
        if len(sonar) >= total:
            break
        if not rows:
            raise ValueError('Sonar inventory is incomplete')
    else:
        raise ValueError('Sonar inventory is incomplete')
    projects = []
    # ADO supports bounded skip/top; a full page requires another page.
    for page in range(max_pages):
        data = await read(ADO_PROJECTS, {'top': 100, 'skip': page * 100})
        rows = data.get('value')
        if not isinstance(rows, list):
            raise ValueError('ADO project inventory is incomplete')
        projects.extend(rows)
        if len(projects) > max_projects:
            raise ValueError('ADO project inventory exceeds configured bound')
        if len(rows) < 100:
            break
    else:
        raise ValueError('ADO project inventory is incomplete')
    names = [p.get('name') for p in projects]
    if any(not isinstance(n, str) or not n for n in names) or len(set(names)) != len(names):
        raise ValueError('ADO project inventory contains repeated or invalid projects')
    repositories = []
    for name in names:
        data = await read(ADO_REPOSITORIES, {'project': name})
        rows = data.get('value')
        if not isinstance(rows, list) or len(repositories) + len(rows) > 5000:
            raise ValueError('ADO repository inventory is incomplete or too large')
        repositories.extend(rows)
    return match_projects(sonar, repositories, explicit)
